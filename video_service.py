"""Approval + video-generation orchestration.

Sits between the content runs (runs/<id>/stages.json — produced by the
existing pipeline, which this module never modifies) and the provider
adapters. Owns the state machine:

  awaiting_review → approved → queued_for_video → generating
                  ↘ rejected                    ↘ completed / generation_failed / cancelled

Editing a script after approval changes its hash, which invalidates the
approval (it drops back to awaiting_review at the next generation attempt).
"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pipeline
import twoshot
import video_prep
from video_providers import ProviderError
from video_store import VideoStore, new_id, now_iso

RUNS_DIR = pipeline.RUNS_DIR

STALE_AFTER_SECONDS = 2 * 60 * 60  # jobs stuck in queued/processing beyond this are marked stale


class VideoService:
    def __init__(self, store: VideoStore, providers: dict, runs_dir: Path | None = None, now=time.time):
        self.store = store
        self.providers = providers
        self.runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
        self.now = now
        self._poll_lock = threading.Lock()
        self.videos_dir = self.store.path.parent / "videos"
        self.idle_cache = self.videos_dir / "_idle_cache"
        self._two_shot_started: set[str] = set()

    # ------------------------------------------------------------------
    # Content runs
    # ------------------------------------------------------------------
    def list_runs(self) -> list[dict]:
        runs = []
        if not self.runs_dir.exists():
            return runs
        for d in sorted(self.runs_dir.iterdir(), reverse=True):
            if d.is_dir() and (d / "stages.json").exists():
                runs.append({"id": d.name, "hasPack": (d / "handoff-pack.md").exists()})
        return runs

    def _read_stages(self, run_id: str) -> dict | None:
        p = self.runs_dir / run_id / "stages.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def get_run(self, run_id: str) -> dict | None:
        stages = self._read_stages(run_id)
        if stages is None:
            return None
        out = {"id": run_id, "scripts": {}, "pack": None}
        pack_path = self.runs_dir / run_id / "handoff-pack.md"
        if pack_path.exists():
            out["pack"] = pack_path.read_text(encoding="utf-8")
        for script_key, synopsis_key in (("scriptA", "synopsisA"), ("scriptB", "synopsisB")):
            raw = stages.get(script_key)
            if not raw:
                continue
            script = video_prep.parse_script(raw)
            synopsis = video_prep.parse_script(stages.get(synopsis_key, "") or "")
            approval = self.store.find_approval(run_id, script_key)
            _, problems = video_prep.validate_for_approval(raw, synopsis)
            persona = video_prep.persona_for_voice((script or {}).get("voice")) or "Product"
            out["scripts"][script_key] = {
                "scriptKey": script_key,
                "parsed": script,
                "scriptHash": video_prep.script_hash(raw),
                "narrationPreview": video_prep.build_narration(script or {}),
                "estimatedSeconds": video_prep.estimate_seconds(
                    video_prep.build_narration(script or {}), (script or {}).get("runtime_seconds")),
                "persona": persona,
                "sources": video_prep.source_records(script or {}, synopsis),
                "gateProblems": problems,
                "reviewStatus": (approval or {}).get("status", "awaiting_review"),
                "approvalId": (approval or {}).get("id"),
            }
        return out

    # ------------------------------------------------------------------
    # Approval workflow
    # ------------------------------------------------------------------
    def approve(self, run_id: str, script_key: str, approved_by: str = "",
                overrides: dict | None = None) -> dict:
        overrides = overrides or {}
        stages = self._read_stages(run_id)
        if stages is None or not stages.get(script_key):
            return {"errors": [f"Run {run_id} has no {script_key}."]}
        raw = stages[script_key]
        synopsis_key = "synopsisA" if script_key == "scriptA" else "synopsisB"
        synopsis = video_prep.parse_script(stages.get(synopsis_key, "") or "")

        script, problems = video_prep.validate_for_approval(raw, synopsis)
        if problems:
            return {"errors": problems}

        settings = self.store.get_settings()
        if video_prep.is_dialogue(script):
            persona = "Dialogue"
            persona_cfg = {sp: dict(settings["personas"].get(sp, {}))
                           for sp in video_prep.DIALOGUE_SPEAKERS}
        else:
            persona = overrides.get("persona") or video_prep.persona_for_voice(script.get("voice")) or "Product"
            persona_cfg = dict(settings["personas"].get(persona, {}))
            for f in ("referenceAssetId", "providerAvatarId", "voiceId"):
                if overrides.get(f):
                    persona_cfg[f] = overrides[f]

        narration = video_prep.build_narration(script, settings.get("pronunciations") or {})
        provider_name = overrides.get("provider") or settings["provider"]

        existing = self.store.find_approval(run_id, script_key)
        approval = {
            "id": (existing or {}).get("id") or new_id("apv"),
            "scriptId": script_key,
            "contentRunId": run_id,
            "scriptVersion": video_prep.script_hash(raw),
            "status": "approved",
            "approvedAt": now_iso(),
            "approvedBy": approved_by or "local user",
            "rejectedAt": None,
            "rejectionReason": None,
            "persona": persona,
            "personaConfigSnapshot": persona_cfg,
            "provider": provider_name,
            "settingsSnapshot": {
                "aspectRatio": overrides.get("aspectRatio") or settings["defaultAspectRatio"],
                "resolution": overrides.get("resolution") or settings["defaultResolution"],
                "captions": overrides.get("captions", settings["captionsEnabled"]),
            },
            "approvedScriptSnapshot": {
                "raw": raw,
                "narration": narration,
                "title": script.get("title_working", ""),
                "pipeline": script.get("pipeline", ""),
                "voice": script.get("voice", ""),
                "runtimeSeconds": script.get("runtime_seconds"),
                "beats": video_prep.beat_manifest(script),
                "format": "podcast_dialogue" if video_prep.is_dialogue(script) else "single",
                "segments": video_prep.dialogue_segments(script, settings.get("pronunciations") or {})
                            if video_prep.is_dialogue(script) else [],
            },
            "sourceValidationSnapshot": video_prep.source_records(script, synopsis),
        }
        if existing:
            approval["createdAt"] = existing.get("createdAt")
        self.store.upsert_approval(approval)
        est = None
        provider = self.providers.get(provider_name)
        if provider:
            try:
                est = provider.getEstimatedCost(
                    video_prep.build_provider_request(approval, "estimate", settings))
            except (ProviderError, Exception):  # noqa: BLE001 — estimate is best-effort
                est = None
        return {"approval": approval, "estimatedCost": est}

    def reject(self, run_id: str, script_key: str, note: str = "", rejected_by: str = "") -> dict:
        stages = self._read_stages(run_id)
        if stages is None or not stages.get(script_key):
            return {"errors": [f"Run {run_id} has no {script_key}."]}
        existing = self.store.find_approval(run_id, script_key) or {
            "id": new_id("apv"), "scriptId": script_key, "contentRunId": run_id,
        }
        existing.update({
            "status": "rejected", "rejectedAt": now_iso(), "rejectionReason": note or None,
            "approvedBy": rejected_by or existing.get("approvedBy") or "local user",
            "scriptVersion": video_prep.script_hash(stages[script_key]),
        })
        self.store.upsert_approval(existing)
        return {"approval": existing}

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    def generate(self, approval_id: str, options: dict) -> dict:
        """Submit an approved script to the provider. Idempotent per idempotencyKey."""
        approval = self.store.get_approval(approval_id)
        if not approval:
            return {"errors": ["Approval not found."]}

        idem = (options.get("idempotencyKey") or "").strip()
        if idem:
            existing_job_id = self.store.idempotent_job(idem)
            if existing_job_id:
                return {"job": self.store.get_job(existing_job_id), "duplicate": True}

        if not options.get("confirm"):
            return {"errors": ["Generation must be explicitly confirmed (confirm: true)."]}

        # Block a second concurrent attempt for the same approval.
        for j in self.store.list_jobs(approval_id):
            if j["status"] in ("queued", "processing"):
                return {"errors": ["A generation for this script is already in progress."]}

        settings = self.store.get_settings()
        # The CURRENT provider setting wins — an approval only remembers which
        # provider was active when it was approved, and must not pin future
        # generations to a stale choice.
        provider_name = options.get("provider") or settings["provider"]
        provider = self.providers.get(provider_name)
        if not provider:
            return {"errors": [f"Unknown provider: {provider_name}"]}
        cfg = provider.validateConfiguration()
        if not cfg.get("ok"):
            return {"errors": [f"{provider.display_name} is not configured: {cfg.get('detail')}"]}

        # Approval must match the CURRENT script on disk (edit invalidates).
        stages = self._read_stages(approval["contentRunId"])
        current_hash = None
        if stages and stages.get(approval["scriptId"]):
            current_hash = video_prep.script_hash(stages[approval["scriptId"]])
        if current_hash is not None and current_hash != approval.get("scriptVersion"):
            approval["status"] = "awaiting_review"
            self.store.upsert_approval(approval)
            return {"errors": ["The script changed after approval. It has been returned to review — approve the current version first."]}

        # Per-generation overrides (aspect ratio for landscape/vertical variants).
        if options.get("aspectRatio"):
            approval["settingsSnapshot"]["aspectRatio"] = options["aspectRatio"]

        # Persona setup (photos, avatar IDs, voices) follows the CURRENT
        # Settings — same rule as the provider choice above. Only the script
        # itself is frozen by the approval, so "fix Settings, hit Retry" works.
        persona_cfg = self._current_persona_cfg(approval)
        approval["personaConfigSnapshot"] = persona_cfg
        voice_problems = self._ensure_voices(approval, persona_cfg, provider)
        problems = voice_problems + video_prep.validate_for_generation(
            approval, current_hash, persona_cfg, provider, settings)
        if problems:
            return {"errors": problems}

        job_id = new_id("job")
        request = video_prep.build_provider_request(approval, job_id, settings)
        estimated = None
        try:
            estimated = provider.getEstimatedCost(request)
        except Exception:  # noqa: BLE001
            estimated = None

        # Cost protection (never applied to the mock provider).
        if not provider.is_mock:
            limit = settings.get("monthlyHardLimit")
            if limit:
                projected = self.store.month_spend() + float(estimated or 0.0)
                if projected > float(limit):
                    return {"errors": [
                        f"Monthly spending limit reached (${self.store.month_spend():.2f} spent, "
                        f"limit ${float(limit):.2f}). Raise the limit in Settings to continue."]}

        job = {
            "id": job_id,
            "approvalId": approval_id,
            "attemptNumber": self.store.next_attempt_number(approval_id),
            "provider": provider_name,
            "providerJobId": "",
            "status": "queued",
            "requestSnapshot": {k: v for k, v in request.items() if k != "spokenScript"} |
                               {"spokenScriptChars": len(request["spokenScript"])},
            "persona": approval["persona"],
            "pipeline": approval["approvedScriptSnapshot"].get("pipeline", ""),
            "title": request["title"],
            "aspectRatio": request["aspectRatio"],
            "resolution": request["resolution"],
            "estimatedCost": estimated,
            "actualCost": None,
            "videoUrl": "", "thumbnailUrl": "", "durationSeconds": None,
            "errorCode": "", "errorMessage": "",
            "queuedAt": now_iso(), "startedAt": None, "completedAt": None,
            "failedAt": None, "cancelledAt": None,
            "isMock": provider.is_mock,
        }
        self.store.add_job(job)
        if idem:
            self.store.remember_idempotency(idem, job_id)
        self.store.add_event(job_id, "submitted", "", {"provider": provider_name,
                                                       "attempt": job["attemptNumber"]})
        try:
            result = provider.createVideo(request)
        except ProviderError as e:
            self.store.update_job(job_id, {"status": "failed", "errorCode": e.code,
                                           "errorMessage": e.message, "failedAt": now_iso()})
            self.store.add_event(job_id, "submit_failed", e.code, {"message": e.message})
            approval["status"] = "generation_failed"
            self.store.upsert_approval(approval)
            return {"errors": [f"Provider rejected the submission: {e.message}"],
                    "job": self.store.get_job(job_id)}

        self.store.update_job(job_id, {
            "providerJobId": result["providerJobId"],
            "status": result["status"] if result["status"] in ("queued", "processing") else "queued",
            "estimatedCost": result.get("estimatedCost", estimated),
            "segments": result.get("segments") or None,
        })
        self.store.add_event(job_id, "accepted", result["status"], {"providerJobId": result["providerJobId"]})
        approval["status"] = "queued_for_video"
        approval["provider"] = provider_name  # record which provider actually ran
        self.store.upsert_approval(approval)
        return {"job": self.store.get_job(job_id)}

    def _current_persona_cfg(self, approval: dict) -> dict:
        """Latest persona config from Settings for the persona(s) this approval needs."""
        personas = self.store.get_settings().get("personas", {})
        if approval.get("persona") == "Dialogue":
            return {sp: dict(personas.get(sp) or {}) for sp in video_prep.DIALOGUE_SPEAKERS}
        return dict(personas.get(approval.get("persona")) or {})

    def _ensure_voices(self, approval: dict, persona_cfg: dict, provider) -> list[str]:
        """Any persona without a voice gets a random one from the provider's
        voice list (private/cloned voices first, then English). The pick is
        saved to Settings so every later generation keeps the same voice.
        Mock-testing placeholder voices count as unset on real providers."""
        dialogue = approval.get("persona") == "Dialogue"
        targets = {sp: persona_cfg.setdefault(sp, {}) for sp in video_prep.DIALOGUE_SPEAKERS} \
            if dialogue else {approval.get("persona"): persona_cfg}

        def _unset(cfg):
            v = (cfg.get("voiceId") or "").strip()
            return not v or (not provider.is_mock and v.lower().startswith("mock"))

        need = {name: cfg for name, cfg in targets.items() if _unset(cfg)}
        if not need:
            return []
        if provider.is_mock:
            for cfg in need.values():
                cfg["voiceId"] = "mock-voice-auto"  # in-memory only; never saved to Settings
            return []
        if not hasattr(provider, "list_voices"):
            return []  # no voice catalog (e.g. Tavus replicas carry their own voice)
        try:
            voices = [v for v in provider.list_voices() if v.get("voiceId")]
        except Exception as e:  # noqa: BLE001 — ProviderError or network failure
            msg = getattr(e, "message", str(e))
            return [f"No voice is set and one could not be auto-picked ({provider.display_name} "
                    f"voice list failed: {msg}). Pick voices in Settings → Browse voices."]
        if not voices:
            return [f"No voice is set and {provider.display_name} returned no voices to pick from. "
                    f"Pick voices in Settings → Browse voices."]
        private = [v for v in voices if v.get("type") == "private"]
        english = [v for v in voices if "english" in (v.get("language") or "").lower()]
        pool = private or english or voices
        taken = {cfg.get("voiceId") for cfg in targets.values() if not _unset(cfg)}
        for name, cfg in need.items():
            candidates = [v for v in pool if v["voiceId"] not in taken] or pool
            pick = random.choice(candidates)
            cfg["voiceId"] = pick["voiceId"]
            taken.add(pick["voiceId"])
            self.store.update_settings({"personas": {name: {"voiceId": pick["voiceId"]}}})
        return []

    def cancel(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            return {"errors": ["Job not found."]}
        if job["status"] not in ("queued", "processing"):
            return {"errors": [f"Job is already {job['status']}."]}
        provider = self.providers.get(job["provider"])
        if not provider:
            return {"errors": [f"Unknown provider: {job['provider']}"]}
        try:
            provider.cancelVideo(job["providerJobId"])
        except ProviderError as e:
            if e.code == "cancel_unsupported":
                return {"errors": [f"{provider.display_name} cannot cancel a submitted video; it will finish and can be discarded."]}
            return {"errors": [e.message]}
        self.store.update_job(job_id, {"status": "cancelled", "cancelledAt": now_iso()})
        self.store.add_event(job_id, "cancelled", "cancelled", {})
        self._sync_approval_status(job["approvalId"])
        return {"job": self.store.get_job(job_id)}

    # ------------------------------------------------------------------
    # Status polling / webhooks
    # ------------------------------------------------------------------
    def poll_active_jobs(self) -> int:
        """Poll the provider for every non-terminal job. Returns jobs updated."""
        if not self._poll_lock.acquire(blocking=False):
            return 0
        try:
            updated = 0
            for job in self.store.active_jobs():
                if self._refresh_job(job):
                    updated += 1
            return updated
        finally:
            self._poll_lock.release()

    def _refresh_job(self, job: dict) -> bool:
        provider = self.providers.get(job["provider"])
        if not provider or not job.get("providerJobId"):
            return False
        # Stale detection
        queued_at = job.get("queuedAt") or job.get("createdAt")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(queued_at)).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age > STALE_AFTER_SECONDS:
            self.store.update_job(job["id"], {
                "status": "failed", "errorCode": "stale",
                "errorMessage": "No terminal status from the provider within 2 hours — marked stale. Retry to submit again.",
                "failedAt": now_iso()})
            self.store.add_event(job["id"], "stale", "", {})
            self._sync_approval_status(job["approvalId"])
            return True
        try:
            if job.get("segments"):
                result = self._poll_segments(job, provider)
            else:
                result = provider.getVideoStatus(job["providerJobId"])
        except ProviderError as e:
            self.store.add_event(job["id"], "poll_error", e.code, {"message": e.message})
            return False
        return self._apply_result(job, result)

    def _poll_segments(self, job: dict, provider) -> dict:
        """Aggregate a multi-clip podcast job: completed only when every
        speaker segment is completed; failed as soon as any segment fails."""
        segments = job["segments"]
        for seg in segments:
            if seg.get("status") in ("completed", "failed", "cancelled"):
                continue
            r = provider.getVideoStatus(seg["providerJobId"])
            seg["status"] = r["status"]
            seg["videoUrl"] = r.get("videoUrl") or seg.get("videoUrl", "")
            seg["thumbnailUrl"] = r.get("thumbnailUrl") or seg.get("thumbnailUrl", "")
            seg["durationSeconds"] = r.get("durationSeconds") or seg.get("durationSeconds")
            seg["errorMessage"] = r.get("errorMessage", "")
        self.store.update_job(job["id"], {"segments": segments})
        statuses = [s.get("status", "queued") for s in segments]
        if any(s == "failed" for s in statuses):
            failed = next(s for s in segments if s.get("status") == "failed")
            return {"providerJobId": job["providerJobId"], "status": "failed",
                    "videoUrl": "", "thumbnailUrl": "", "durationSeconds": None,
                    "estimatedCost": None, "actualCost": None,
                    "errorCode": "segment_failed",
                    "errorMessage": f"Segment {failed['index'] + 1} ({failed['speaker']}) failed: {failed.get('errorMessage') or 'provider error'}"}
        if all(s == "completed" for s in statuses):
            total = sum(s.get("durationSeconds") or 0 for s in segments) or None
            return {"providerJobId": job["providerJobId"], "status": "completed",
                    "videoUrl": segments[0].get("videoUrl", ""),
                    "thumbnailUrl": segments[0].get("thumbnailUrl", ""),
                    "durationSeconds": total, "estimatedCost": None, "actualCost": None,
                    "errorCode": "", "errorMessage": ""}
        status = "processing" if any(s == "processing" for s in statuses) else "queued"
        return {"providerJobId": job["providerJobId"], "status": status,
                "videoUrl": "", "thumbnailUrl": "", "durationSeconds": None,
                "estimatedCost": None, "actualCost": None, "errorCode": "", "errorMessage": ""}

    def _apply_result(self, job: dict, result: dict) -> bool:
        status = result["status"]
        if status == job["status"] and status not in ("completed",):
            return False
        patch = {"status": status}
        if status == "processing" and not job.get("startedAt"):
            patch["startedAt"] = now_iso()
        if status == "completed":
            patch.update({
                "completedAt": job.get("completedAt") or now_iso(),
                "videoUrl": result.get("videoUrl") or job.get("videoUrl"),
                "thumbnailUrl": result.get("thumbnailUrl") or job.get("thumbnailUrl"),
                "durationSeconds": result.get("durationSeconds") or job.get("durationSeconds"),
                "actualCost": result.get("actualCost", job.get("actualCost")),
            })
        if status == "failed":
            patch.update({"failedAt": now_iso(),
                          "errorCode": result.get("errorCode", ""),
                          "errorMessage": result.get("errorMessage", "")})
        if status == "cancelled":
            patch["cancelledAt"] = now_iso()
        self.store.update_job(job["id"], patch)
        self.store.add_event(job["id"], "status", status, {
            k: v for k, v in result.items() if k in ("errorCode", "errorMessage", "durationSeconds")})
        self._sync_approval_status(job["approvalId"])
        if result["status"] == "completed":
            self._maybe_start_two_shot(job["id"])
        return True

    # ------------------------------------------------------------------
    # Two-shot ("same room") compositing — automatic after a dialogue job
    # completes. Runs in a background thread so polling is never blocked.
    # ------------------------------------------------------------------
    def _maybe_start_two_shot(self, job_id: str):
        job = self.store.get_job(job_id)
        if not job or job.get("isMock"):
            return
        if job.get("persona") != "Dialogue" or not job.get("segments"):
            return
        existing = job.get("twoShot") or {}
        if existing.get("status") in ("processing", "ready"):
            return
        if job_id in self._two_shot_started:
            return
        self._two_shot_started.add(job_id)
        self.store.update_job(job_id, {"twoShot": {"status": "processing",
                                                   "startedAt": now_iso()}})
        self.store.add_event(job_id, "two_shot_started", "", {})
        threading.Thread(target=self._two_shot_worker, args=(job_id,), daemon=True).start()

    def rebuild_two_shot(self, job_id: str) -> dict:
        """Re-run the stitch for a completed dialogue job — after retuning
        twoshot.ALIGN or bumping IDLE_VERSION — reusing the downloaded segment
        clips (no paid re-generation of the dialogue)."""
        job = self.store.get_job(job_id)
        if not job:
            return {"errors": ["Job not found."]}
        if job.get("persona") != "Dialogue" or not job.get("segments"):
            return {"errors": ["Not a dialogue job — nothing to stitch."]}
        if job.get("status") != "completed":
            return {"errors": ["The job has not completed; the stitch runs automatically when it does."]}
        if (job.get("twoShot") or {}).get("status") == "processing" or job_id in self._two_shot_started:
            return {"errors": ["A stitch for this job is already running."]}
        self.store.update_job(job_id, {"twoShot": {}})
        self._maybe_start_two_shot(job_id)
        return {"ok": True, "job": self.store.get_job(job_id)}

    def _two_shot_worker(self, job_id: str):
        try:
            job = self.store.get_job(job_id)
            segments = job.get("segments") or []
            turns = sorted(({"index": s["index"], "speaker": s["speaker"]} for s in segments),
                           key=lambda t: t["index"])
            sides = twoshot.assign_sides(turns)
            if len(sides) < 2:
                raise twoshot.StitchError("a two-shot needs exactly two distinct speakers.")

            req_segs = (job.get("requestSnapshot") or {}).get("segments") or []
            cfg_by_speaker: dict = {}
            for rs in req_segs:
                cfg_by_speaker.setdefault(rs["speaker"], rs)

            out_dir = self.videos_dir / job_id
            out_dir.mkdir(parents=True, exist_ok=True)

            seg_clips = {}
            for s in segments:
                if not s.get("videoUrl"):
                    raise twoshot.StitchError(f"segment {s['index']} ({s['speaker']}) has no video URL.")
                dest = out_dir / f"seg{s['index']}_{s['speaker']}.mp4"
                if not dest.exists():
                    twoshot.download(s["videoUrl"], dest)
                seg_clips[s["index"]] = dest

            provider = self.providers.get(job["provider"])
            idle_sources = {spk: self._ensure_idle_loop(spk, cfg_by_speaker.get(spk, {}), job, provider)
                            for spk in sides}

            out_path = out_dir / "two_shot.mp4"
            res = twoshot.render(turns, seg_clips, idle_sources, sides, out_path)
            self.store.update_job(job_id, {"twoShot": {
                "status": "ready", "path": res["path"],
                "durationSeconds": res["durationSeconds"],
                "url": f"/api/video/two-shot/{job_id}",
                "completedAt": now_iso()}})
            self.store.add_event(job_id, "two_shot_ready", "",
                                 {"durationSeconds": res["durationSeconds"]})
        except Exception as e:  # noqa: BLE001 — surfaced on the job, never crashes the poller
            self.store.update_job(job_id, {"twoShot": {
                "status": "failed", "error": str(e), "failedAt": now_iso()}})
            self.store.add_event(job_id, "two_shot_failed", "", {"message": str(e)})
        finally:
            self._two_shot_started.discard(job_id)

    def _ensure_idle_loop(self, speaker: str, cfg: dict, job: dict, provider) -> Path:
        """A muted 'listening' source clip for one host, cached per avatar+aspect
        so it is generated once and reused by every later run. The render step
        turns it into a varied non-repeating track (twoshot.build_humanized_idle),
        so only the raw clip is cached. Key is versioned: bumping
        twoshot.IDLE_VERSION regenerates idles when the script/prompt changes."""
        avatar_key = cfg.get("avatarId") or cfg.get("referenceAssetId") or speaker
        aspect = job.get("aspectRatio") or "16:9"
        key = f"{avatar_key}_{aspect.replace(':', 'x')}_v{twoshot.IDLE_VERSION}"
        self.idle_cache.mkdir(parents=True, exist_ok=True)
        raw_path = self.idle_cache / f"{key}_raw.mp4"
        if not raw_path.exists():
            request = {
                "voiceId": cfg.get("voiceId"),
                "spokenScript": twoshot.IDLE_SCRIPT,
                "resolution": job.get("resolution") or "1080p",
                "aspectRatio": aspect,
                "title": f"idle — {speaker}",
                "jobId": f"{job['id']}-idle-{speaker}",
                "captions": {"enabled": False},
                "avatarId": cfg.get("avatarId"),
                "referenceAssetId": cfg.get("referenceAssetId"),
                "expressiveness": "low",
                "motionPrompt": twoshot.IDLE_MOTION_PROMPT,
            }
            result = provider.createVideo(request)
            url = self._await_single(provider, result["providerJobId"])
            twoshot.download(url, raw_path)
        return raw_path

    def _await_single(self, provider, provider_job_id: str,
                      interval: int = 12, max_wait: int = 900) -> str:
        """Poll one provider clip to completion and return its video URL."""
        waited = 0
        while True:
            r = provider.getVideoStatus(provider_job_id)
            status = r.get("status")
            if status == "completed":
                url = r.get("videoUrl")
                if not url and hasattr(provider, "refreshOutputUrl"):
                    url = provider.refreshOutputUrl(provider_job_id).get("videoUrl")
                if not url:
                    raise twoshot.StitchError("idle clip completed but returned no video URL.")
                return url
            if status == "failed":
                raise twoshot.StitchError(f"idle clip failed: {r.get('errorMessage', '')}")
            if waited >= max_wait:
                raise twoshot.StitchError("idle clip generation timed out.")
            time.sleep(interval)
            waited += interval

    def _sync_approval_status(self, approval_id: str):
        approval = self.store.get_approval(approval_id)
        if not approval:
            return
        jobs = self.store.list_jobs(approval_id)
        if not jobs:
            return
        latest = jobs[-1]
        mapping = {"queued": "queued_for_video", "processing": "generating",
                   "completed": "completed", "failed": "generation_failed",
                   "cancelled": "cancelled"}
        new_status = mapping.get(latest["status"])
        if new_status and approval.get("status") != new_status:
            approval["status"] = new_status
            self.store.upsert_approval(approval)

    def handle_webhook(self, provider_name: str, headers: dict, raw_body: bytes) -> dict:
        provider = self.providers.get(provider_name)
        if not provider:
            raise ProviderError("unknown_provider", f"Unknown webhook provider: {provider_name}")
        event = provider.normalizeWebhook(headers, raw_body)  # raises on bad signature
        if self.store.webhook_seen(provider_name, event["eventId"]):
            return {"ok": True, "duplicate": True}

        job = None
        for j in self.store.list_jobs():
            if j.get("providerJobId") == event.get("providerJobId") or \
               (event.get("callbackId") and j["id"] == event["callbackId"]):
                job = j
                break
        if not job:
            return {"ok": True, "unmatched": True}

        if event.get("requiresRepoll") or not event.get("status"):
            self._refresh_job(job)
        else:
            result = {"providerJobId": job["providerJobId"], "status": event["status"],
                      "videoUrl": event.get("videoUrl", ""), "thumbnailUrl": "",
                      "durationSeconds": None, "estimatedCost": None, "actualCost": None,
                      "errorCode": "", "errorMessage": event.get("errorMessage", "")}
            # Trust the signature-verified event, then confirm details by re-polling.
            self._apply_result(job, result)
            refreshed = self.store.get_job(job["id"])
            if refreshed and refreshed["status"] in ("completed",) and not refreshed.get("videoUrl"):
                self._refresh_job(refreshed)
        return {"ok": True}

    def refresh_output(self, job_id: str) -> dict:
        """Presigned output URLs expire — re-fetch from the provider on demand."""
        job = self.store.get_job(job_id)
        if not job:
            return {"errors": ["Job not found."]}
        provider = self.providers.get(job["provider"])
        if not provider or not job.get("providerJobId"):
            return {"errors": ["Job has no provider reference."]}
        try:
            result = provider.refreshOutputUrl(job["providerJobId"])
        except ProviderError as e:
            return {"errors": [e.message]}
        self._apply_result(job, result)
        return {"job": self.store.get_job(job_id)}

    # ------------------------------------------------------------------
    # State for the dashboard
    # ------------------------------------------------------------------
    def state(self) -> dict:
        settings = self.store.get_settings()
        import os as _os
        provider_status = {}
        for name, p in self.providers.items():
            cfg = p.validateConfiguration()
            key = ""
            for var in getattr(p, "key_env_vars", ()):
                key = (_os.environ.get(var) or "").strip()
                if key:
                    break
            provider_status[name] = {"displayName": p.display_name, "isMock": p.is_mock,
                                     "supportsReferenceImage": p.supports_reference_image,
                                     "supportsCancel": p.supports_cancel,
                                     "supportedAspects": list(p.supported_aspects),
                                     "keyConfigured": bool(key),
                                     "keyHint": ("••••" + key[-4:]) if key else "",
                                     "acceptsKey": bool(getattr(p, "key_env_vars", ())), **cfg}
        personas = {}
        active = self.providers.get(settings["provider"])
        real_provider = active is not None and not active.is_mock
        for name, cfg in settings["personas"].items():
            asset = self.store.get_asset(cfg["referenceAssetId"]) if cfg.get("referenceAssetId") else None
            # Mock-testing placeholder IDs count as unset once a real provider is active.
            avatar = (cfg.get("providerAvatarId") or "")
            avatar_ok = bool(avatar) and not (real_provider and avatar.lower().startswith("mock"))
            voice = (cfg.get("voiceId") or "")
            voice_ok = bool(voice) and not (real_provider and voice.lower().startswith("mock"))
            if not asset and not avatar_ok:
                readiness = "not_configured"
            elif voice_ok:
                readiness = "fully_ready"
            else:
                readiness = "voice_not_configured"
            personas[name] = {**cfg, "asset": asset, "readiness": readiness}
        return {
            "settings": settings,
            "providerStatus": provider_status,
            "activeProviderIsMock": self.providers.get(settings["provider"], None) is not None
                                    and self.providers[settings["provider"]].is_mock,
            "personas": personas,
            "approvals": self.store.list_approvals(),
            "jobs": sorted(self.store.list_jobs(), key=lambda j: j.get("createdAt", ""), reverse=True),
            "monthSpend": self.store.month_spend(),
            "webhookConfigured": bool((__import__("os").environ.get("VIDEO_WEBHOOK_PUBLIC_URL") or "").strip()),
        }
