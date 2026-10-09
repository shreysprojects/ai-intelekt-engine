"""Video preparation layer: turns an approved handoff-pack script into clean
provider input, and enforces the validation gate before approval/generation.

Narration sent to a provider contains ONLY the spoken voice-over lines.
It must never contain markdown, tables, JSON, URLs, source lists, beat names,
[INFERRED]/[VERIFY]/DO NOT USE markers, or internal production notes.
"""
from __future__ import annotations

import hashlib
import json
import re

import pipeline

WORDS_PER_SECOND = 2.5  # conservative spoken pace for runtime estimation

BLOCKED_NARRATION_TOKENS = ("[VERIFY", "DO NOT USE", "[INFERRED]", "http://", "https://")
BEAT_NAMES = {"HOOK", "TENSION", "SHIFT", "PROOF", "CTA", "CONTEXT", "POV", "IMPLICATION", "CLOSE",
              "OPEN", "EXCHANGE", "MOVE"}

VOICE_TO_PERSONA = {"ravi": "Ravi", "rik": "Rik", "product": "Product",
                    "dialogue": "Dialogue", "ravi & rik": "Dialogue", "ravi+rik": "Dialogue"}
DIALOGUE_SPEAKERS = ("Ravi", "Rik")


def is_dialogue(script: dict) -> bool:
    """Podcast-format scripts carry per-beat speakers (Ravi/Rik co-hosts)."""
    if not script:
        return False
    if script.get("format") == "podcast_dialogue":
        return True
    return any(b.get("speaker") for b in script.get("beats", []) if isinstance(b, dict))


def script_hash(raw_script_text: str) -> str:
    """Version fingerprint of the exact script text; changing the script
    changes the hash and invalidates any approval made against it."""
    return hashlib.sha256(raw_script_text.encode("utf-8")).hexdigest()


def parse_script(raw_text: str) -> dict | None:
    try:
        return json.loads(pipeline.extract_json(raw_text))
    except (json.JSONDecodeError, ValueError):
        return None


def persona_for_voice(voice: str | None) -> str | None:
    return VOICE_TO_PERSONA.get((voice or "").strip().lower())


def build_narration(script: dict, pronunciations: dict | None = None) -> str:
    """Spoken narration = the vo lines only, in beat order.
    Dialogue scripts get speaker labels (for captions/audit); the labels are
    never sent to a voice — per-speaker TTS text comes from dialogue_segments().
    """
    lines = []
    for beat in script.get("beats", []):
        vo = (beat.get("vo") or "").strip()
        if vo:
            speaker = (beat.get("speaker") or "").strip()
            lines.append(f"{speaker}: {vo}" if speaker else vo)
    narration = "\n\n".join(lines)
    for word, spoken in (pronunciations or {}).items():
        if word and spoken:
            narration = narration.replace(word, spoken)
    return narration


def dialogue_segments(script: dict, pronunciations: dict | None = None) -> list[dict]:
    """Group consecutive same-speaker turns into video segments.
    Each segment becomes one provider clip (that speaker's avatar + voice),
    delivered in order for stitching into the podcast cut."""
    segments: list[dict] = []
    for beat in script.get("beats", []):
        vo = (beat.get("vo") or "").strip()
        speaker = (beat.get("speaker") or "").strip()
        if not vo:
            continue
        for word, spoken in (pronunciations or {}).items():
            if word and spoken:
                vo = vo.replace(word, spoken)
        if segments and segments[-1]["speaker"] == speaker:
            segments[-1]["text"] += "\n\n" + vo
        else:
            segments.append({"index": len(segments), "speaker": speaker, "text": vo})
    return segments


def beat_manifest(script: dict) -> list:
    return [
        {
            "beat": b.get("beat", ""),
            "speaker": b.get("speaker", ""),
            "vo": b.get("vo", ""),
            "onScreenText": b.get("on_screen_text", ""),
            "visualDirection": b.get("visual_direction", ""),
        }
        for b in script.get("beats", [])
    ]


def estimate_seconds(narration: str, declared: int | None = None) -> int:
    words = len(narration.split())
    est = round(words / WORDS_PER_SECOND)
    if declared and declared > 0:
        return max(est, int(declared))
    return est


_STAT_RE = re.compile(r"(\d[\d,.]*\s*(?:%|percent|billion|million|crore|bn|b\b)|\$\s?\d[\d,.]*)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> set:
    return {m.replace(",", "") for m in _NUM_RE.findall(text or "")}


def find_unsupported_stats(narration: str, fact_texts: list[str]) -> list[str]:
    """Digit-based claims in the narration must appear in an approved fact.

    Limitation (documented): numbers written out as words ("ninety percent")
    are not machine-checked here; they were already gated upstream by the
    script agent's approved_facts rule and by human review.
    """
    allowed = set()
    for t in fact_texts:
        allowed |= _numbers_in(t)
    problems = []
    for stat in _STAT_RE.findall(narration):
        nums = _numbers_in(stat)
        if nums and not nums <= allowed:
            problems.append(stat.strip())
    return problems


def gather_fact_texts(script: dict, synopsis: dict | None) -> list[str]:
    texts = [f.get("fact", "") for f in script.get("facts_used", []) if isinstance(f, dict)]
    if synopsis:
        texts += [f.get("fact", "") for f in synopsis.get("approved_facts", []) if isinstance(f, dict)]
    return [t for t in texts if t]


def _match_approved_fact(fact_text: str, synopsis: dict | None) -> dict | None:
    """Best-overlap match of a used fact against the router's approved facts."""
    if not synopsis:
        return None
    words = set(re.findall(r"[a-z0-9%$]+", fact_text.lower()))
    best, best_score = None, 0.0
    for af in synopsis.get("approved_facts", []):
        aw = set(re.findall(r"[a-z0-9%$]+", (af.get("fact") or "").lower()))
        if not aw or not words:
            continue
        score = len(words & aw) / max(len(words), 1)
        if score > best_score:
            best, best_score = af, score
    return best if best_score >= 0.3 else None


def validate_for_approval(raw_script_text: str, synopsis: dict | None) -> tuple[dict | None, list[str]]:
    """Validation gate for 'Approve for Video'. Returns (parsed_script, problems)."""
    problems: list[str] = []
    script = parse_script(raw_script_text)
    if not script:
        return None, ["Script could not be parsed as JSON — re-run the pipeline for this run."]

    beats = script.get("beats") or []
    if not beats:
        problems.append("Script has no beats.")
    narration = build_narration(script)
    if not narration.strip():
        problems.append("Script has no spoken narration (empty vo lines).")

    whole_text = raw_script_text
    if "[VERIFY" in whole_text:
        problems.append("Script contains an unresolved [VERIFY] marker — resolve it before approval.")
    if "DO NOT USE" in narration:
        problems.append("Narration contains a DO NOT USE item — remove it before approval.")

    for token in BLOCKED_NARRATION_TOKENS:
        if token in narration:
            problems.append(f"Narration contains blocked content: {token}")
    for line in narration.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "|", "{", "```")):
            problems.append(f"Narration contains non-spoken formatting: {stripped[:40]!r}")
            break
        if stripped.upper() in BEAT_NAMES:
            problems.append(f"Narration contains a beat name: {stripped}")
            break

    if is_dialogue(script):
        speakers = {(b.get("speaker") or "").strip() for b in beats if isinstance(b, dict)}
        bad = speakers - set(DIALOGUE_SPEAKERS)
        if bad:
            problems.append(f"Dialogue has unknown speaker(s): {', '.join(sorted(x or '(blank)' for x in bad))} — only Ravi and Rik can host.")
        if len(speakers & set(DIALOGUE_SPEAKERS)) < 2:
            problems.append("A podcast script needs both hosts — at least one turn each for Ravi and Rik.")

    facts_used = [f for f in script.get("facts_used", []) if isinstance(f, dict) and f.get("fact")]
    for f in facts_used:
        if not (f.get("source") or "").strip():
            problems.append(f"Fact lacks a source record: {f['fact'][:60]!r}")
        match = _match_approved_fact(f["fact"], synopsis)
        if match is not None and match.get("verified") is False:
            problems.append(f"A required source is unverified: {f['fact'][:60]!r}")

    fact_texts = gather_fact_texts(script, synopsis)
    for stat in find_unsupported_stats(narration, fact_texts):
        problems.append(f"Narration contains a statistic not backed by an approved source: {stat!r}")

    # De-duplicate while preserving order
    seen, unique = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return script, unique


def source_records(script: dict, synopsis: dict | None) -> list[dict]:
    """Audit copy of sources retained with the approval (never narrated)."""
    records = []
    for f in script.get("facts_used", []) or []:
        if isinstance(f, dict):
            records.append({"fact": f.get("fact", ""), "source": f.get("source", ""),
                            "url": "", "verified": None})
    if synopsis:
        for af in synopsis.get("approved_facts", []) or []:
            records.append({"fact": af.get("fact", ""), "source": af.get("source", ""),
                            "url": af.get("url", ""), "verified": af.get("verified")})
    if script.get("source_story_url"):
        records.append({"fact": "(source story)", "source": "", "url": script["source_story_url"],
                        "verified": None})
    return records


def validate_for_generation(approval: dict, current_hash: str | None, persona_cfg: dict,
                            provider, settings: dict) -> list[str]:
    """Pre-flight checks immediately before submitting to a provider."""
    problems = []
    if approval.get("status") not in ("approved", "generation_failed", "completed", "cancelled"):
        problems.append(f"Script is not approved (status: {approval.get('status')}).")
    if current_hash is not None and approval.get("scriptVersion") != current_hash:
        problems.append("The script changed after approval — re-review and approve the current version.")
    narration = approval.get("approvedScriptSnapshot", {}).get("narration", "")
    if not narration.strip():
        problems.append("Approved narration is empty.")
    if "[VERIFY" in narration or "DO NOT USE" in narration:
        problems.append("Approved narration contains unresolved validation markers.")

    if approval.get("persona") == "Dialogue":
        if not getattr(provider, "supports_dialogue", False):
            problems.append(f"{getattr(provider, 'display_name', 'This provider')} cannot generate a two-host podcast; use a provider with dialogue-segment support.")
        for speaker in DIALOGUE_SPEAKERS:
            cfg = (persona_cfg or {}).get(speaker) or {}
            if not cfg.get("providerAvatarId") and not cfg.get("referenceAssetId"):
                problems.append(f"Host '{speaker}' has no reference image or provider avatar configured (Settings).")
            if not cfg.get("voiceId"):
                problems.append(f"Host '{speaker}' has no voice configured (Settings).")
    else:
        if not persona_cfg.get("providerAvatarId") and not persona_cfg.get("referenceAssetId"):
            problems.append(f"Persona '{approval.get('persona')}' has no reference image or provider avatar configured (Settings).")
        if not persona_cfg.get("voiceId"):
            problems.append(f"Persona '{approval.get('persona')}' has no voice configured (Settings).")

    # A mock placeholder ID must never reach a paid provider — catch it here
    # with a plain instruction instead of letting the provider 404.
    if not getattr(provider, "is_mock", False):
        def _placeholder_check(label: str, cfg: dict):
            for field, noun in (("providerAvatarId", "avatar"), ("voiceId", "voice")):
                if field == "providerAvatarId" and cfg.get("referenceAssetId"):
                    continue  # an uploaded photo takes priority, so the avatar ID is never sent
                val = (cfg.get(field) or "")
                if val.lower().startswith("mock"):
                    fix = " — set a real provider ID in Settings" + \
                          (" (or clear it and upload a reference photo)." if field == "providerAvatarId"
                           else " (Settings → Browse voices).")
                    problems.append(f"{label} {noun} ID '{val}' is a mock-testing placeholder{fix}")
        if approval.get("persona") == "Dialogue":
            for sp in DIALOGUE_SPEAKERS:
                _placeholder_check(f"Host {sp}", (persona_cfg or {}).get(sp) or {})
        else:
            _placeholder_check(f"Persona {approval.get('persona')}", persona_cfg or {})

    est = estimate_seconds(narration, approval.get("approvedScriptSnapshot", {}).get("runtimeSeconds"))
    limit = getattr(provider, "max_duration_seconds", None)
    if limit and est > limit:
        problems.append(f"Estimated runtime {est}s exceeds the provider limit of {limit}s.")

    aspect = approval.get("settingsSnapshot", {}).get("aspectRatio") or settings.get("defaultAspectRatio")
    if aspect not in getattr(provider, "supported_aspects", ("16:9", "9:16", "1:1")):
        problems.append(f"Aspect ratio {aspect} is not supported by {getattr(provider, 'name', 'the provider')}.")
    return problems


def build_provider_request(approval: dict, job_id: str, settings: dict) -> dict:
    """Normalized request handed to a provider adapter."""
    snap = approval["approvedScriptSnapshot"]
    persona_cfg = approval.get("personaConfigSnapshot", {})
    s = approval.get("settingsSnapshot", {})
    segments = None
    if approval.get("persona") == "Dialogue":
        segments = []
        for seg in snap.get("segments", []):
            cfg = (persona_cfg or {}).get(seg["speaker"]) or {}
            segments.append({
                "index": seg["index"], "speaker": seg["speaker"], "text": seg["text"],
                "avatarId": cfg.get("providerAvatarId"),
                "referenceAssetId": cfg.get("referenceAssetId"),
                "voiceId": cfg.get("voiceId"),
            })
    return {
        "segments": segments,
        "scriptId": approval["scriptId"],
        "contentRunId": approval["contentRunId"],
        "jobId": job_id,
        "title": snap.get("title") or f"{approval['persona']} — {approval['contentRunId']}",
        "persona": approval["persona"],
        "spokenScript": snap["narration"],
        "referenceAssetId": None if segments else persona_cfg.get("referenceAssetId"),
        "avatarId": None if segments else persona_cfg.get("providerAvatarId"),
        "voiceId": None if segments else persona_cfg.get("voiceId"),
        "aspectRatio": s.get("aspectRatio") or settings.get("defaultAspectRatio", "16:9"),
        "resolution": s.get("resolution") or settings.get("defaultResolution", "1080p"),
        "visualStyle": "executive-podcast",
        "expressiveness": settings.get("expressiveness", "medium"),
        "captions": {"enabled": bool(s.get("captions", settings.get("captionsEnabled", True))), "burnedIn": True},
        "estimatedSeconds": estimate_seconds(snap["narration"], snap.get("runtimeSeconds")),
        "metadata": {
            "pipeline": snap.get("pipeline", ""),
            "scriptVersion": approval.get("scriptVersion", ""),
            "sourceUrls": [r.get("url") for r in approval.get("sourceValidationSnapshot", []) if r.get("url")],
        },
    }
