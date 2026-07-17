"""Persistence for the video-production workflow.

The project has no database; runs are already stored as files. This module
keeps the same approach: one JSON document (data/video_store.json) written
atomically under a lock, plus uploaded reference images in data/assets/.

No provider secrets are ever written here — API keys live in environment
variables only (see .env.example).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ASSETS_DIR = DATA_DIR / "assets"
STORE_PATH = DATA_DIR / "video_store.json"

PERSONAS = ("Ravi", "Rik", "Product")

APPROVAL_STATUSES = (
    "draft", "awaiting_review", "approved", "rejected",
    "queued_for_video", "generating", "completed", "generation_failed", "cancelled",
)
JOB_STATUSES = ("queued", "processing", "completed", "failed", "cancelled")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _default_settings() -> dict:
    return {
        "provider": os.environ.get("VIDEO_PROVIDER", "mock"),
        "fallbackProvider": os.environ.get("VIDEO_FALLBACK_PROVIDER", "tavus"),
        "defaultAspectRatio": os.environ.get("VIDEO_DEFAULT_ASPECT_RATIO", "16:9"),
        "defaultResolution": os.environ.get("VIDEO_DEFAULT_RESOLUTION", "1080p"),
        "captionsEnabled": os.environ.get("VIDEO_CAPTIONS_ENABLED", "true").lower() != "false",
        "costWarningThreshold": _env_float("VIDEO_COST_WARNING_THRESHOLD"),
        "monthlyHardLimit": _env_float("VIDEO_MONTHLY_HARD_LIMIT"),
        "expressiveness": "medium",
        "pronunciations": {},  # spoken replacements, e.g. {"ai-InteleKt": "A I Intellect"}
        "personas": {
            p: {
                "referenceAssetId": None,
                "providerAvatarId": os.environ.get(f"{p.upper()}_VIDEO_AVATAR_ID") or None,
                "voiceId": os.environ.get(f"{p.upper()}_VIDEO_VOICE_ID") or None,
            }
            for p in PERSONAS
        },
    }


def _env_float(name: str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class VideoStore:
    """Thread-safe JSON-file store with atomic writes."""

    def __init__(self, path: Path | None = None, assets_dir: Path | None = None):
        self.path = Path(path) if path else STORE_PATH
        self.assets_dir = Path(assets_dir) if assets_dir else ASSETS_DIR
        self._lock = threading.RLock()
        self._data = None
        self._load()

    # ---------- core ----------
    def _load(self):
        with self._lock:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self._data = {
                    "approvals": [], "jobs": [], "events": [], "assets": [],
                    "settings": _default_settings(), "idempotency": {}, "webhookEvents": [],
                }
                self._save()
            # forward-compatible defaults for keys added later
            defaults = _default_settings()
            for k, v in defaults.items():
                self._data["settings"].setdefault(k, v)
            for p in PERSONAS:
                self._data["settings"]["personas"].setdefault(p, defaults["personas"][p])

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def mutate(self, fn):
        """Run fn(data) under the lock and persist. Returns fn's result."""
        with self._lock:
            result = fn(self._data)
            self._save()
            return result

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ---------- settings ----------
    def get_settings(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data["settings"]))

    def update_settings(self, patch: dict) -> dict:
        allowed = {"provider", "fallbackProvider", "defaultAspectRatio", "defaultResolution",
                   "captionsEnabled", "costWarningThreshold", "monthlyHardLimit",
                   "expressiveness", "pronunciations"}

        def _apply(data):
            for k, v in patch.items():
                if k in allowed:
                    data["settings"][k] = v
            if isinstance(patch.get("personas"), dict):
                for p, cfg in patch["personas"].items():
                    if p in PERSONAS and isinstance(cfg, dict):
                        for f in ("referenceAssetId", "providerAvatarId", "voiceId"):
                            if f in cfg:
                                data["settings"]["personas"][p][f] = cfg[f] or None
            return json.loads(json.dumps(data["settings"]))
        return self.mutate(_apply)

    # ---------- approvals ----------
    def find_approval(self, run_id: str, script_key: str) -> dict | None:
        with self._lock:
            matches = [a for a in self._data["approvals"]
                       if a["contentRunId"] == run_id and a["scriptId"] == script_key]
            return json.loads(json.dumps(matches[-1])) if matches else None

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            for a in self._data["approvals"]:
                if a["id"] == approval_id:
                    return json.loads(json.dumps(a))
            return None

    def upsert_approval(self, approval: dict):
        def _apply(data):
            for i, a in enumerate(data["approvals"]):
                if a["id"] == approval["id"]:
                    approval["updatedAt"] = now_iso()
                    data["approvals"][i] = approval
                    return approval
            approval.setdefault("createdAt", now_iso())
            approval["updatedAt"] = now_iso()
            data["approvals"].append(approval)
            return approval
        return self.mutate(_apply)

    def list_approvals(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["approvals"]))

    # ---------- jobs ----------
    def add_job(self, job: dict) -> dict:
        def _apply(data):
            job.setdefault("createdAt", now_iso())
            job["updatedAt"] = now_iso()
            data["jobs"].append(job)
            return job
        return self.mutate(_apply)

    def update_job(self, job_id: str, patch: dict) -> dict | None:
        def _apply(data):
            for j in data["jobs"]:
                if j["id"] == job_id:
                    j.update(patch)
                    j["updatedAt"] = now_iso()
                    return json.loads(json.dumps(j))
            return None
        return self.mutate(_apply)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            for j in self._data["jobs"]:
                if j["id"] == job_id:
                    return json.loads(json.dumps(j))
            return None

    def list_jobs(self, approval_id: str | None = None) -> list:
        with self._lock:
            jobs = self._data["jobs"]
            if approval_id:
                jobs = [j for j in jobs if j["approvalId"] == approval_id]
            return json.loads(json.dumps(jobs))

    def active_jobs(self) -> list:
        with self._lock:
            return json.loads(json.dumps(
                [j for j in self._data["jobs"] if j["status"] in ("queued", "processing")]))

    def next_attempt_number(self, approval_id: str) -> int:
        with self._lock:
            return 1 + sum(1 for j in self._data["jobs"] if j["approvalId"] == approval_id)

    # ---------- job events ----------
    def add_event(self, job_id: str, event_type: str, provider_status: str = "", details: dict | None = None):
        def _apply(data):
            data["events"].append({
                "id": new_id("evt"), "jobId": job_id, "eventType": event_type,
                "providerStatus": provider_status, "normalizedDetails": details or {},
                "createdAt": now_iso(),
            })
        return self.mutate(_apply)

    def list_events(self, job_id: str) -> list:
        with self._lock:
            return json.loads(json.dumps([e for e in self._data["events"] if e["jobId"] == job_id]))

    # ---------- webhook dedupe ----------
    def webhook_seen(self, provider: str, event_id: str) -> bool:
        """Record a webhook delivery id; returns True if it was already seen."""
        key = f"{provider}:{event_id}"

        def _apply(data):
            if key in data["webhookEvents"]:
                return True
            data["webhookEvents"].append(key)
            del data["webhookEvents"][:-500]  # keep the tail bounded
            return False
        return self.mutate(_apply)

    # ---------- idempotency ----------
    def idempotent_job(self, key: str) -> str | None:
        with self._lock:
            return self._data["idempotency"].get(key)

    def remember_idempotency(self, key: str, job_id: str):
        def _apply(data):
            data["idempotency"][key] = job_id
            if len(data["idempotency"]) > 500:
                for k in list(data["idempotency"])[:-500]:
                    del data["idempotency"][k]
        return self.mutate(_apply)

    # ---------- reference assets ----------
    def add_asset(self, persona: str, filename: str, content: bytes, mime: str,
                  width: int | None, height: int | None) -> dict:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        ext = ".png" if mime == "image/png" else ".jpg"
        asset_id = new_id("asset")
        (self.assets_dir / (asset_id + ext)).write_bytes(content)
        asset = {
            "id": asset_id, "persona": persona, "storageKey": asset_id + ext,
            "mimeType": mime, "fileSize": len(content), "width": width, "height": height,
            "providerAvatarId": None, "providerStatus": "uploaded",
            "originalFilename": filename, "createdAt": now_iso(), "updatedAt": now_iso(),
        }
        self.mutate(lambda d: d["assets"].append(asset))
        return json.loads(json.dumps(asset))

    def get_asset(self, asset_id: str) -> dict | None:
        with self._lock:
            for a in self._data["assets"]:
                if a["id"] == asset_id:
                    return json.loads(json.dumps(a))
            return None

    def asset_bytes(self, asset_id: str) -> tuple[bytes, str] | None:
        a = self.get_asset(asset_id)
        if not a:
            return None
        p = self.assets_dir / a["storageKey"]
        if not p.exists():
            return None
        return p.read_bytes(), a["mimeType"]

    def list_assets(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["assets"]))

    # ---------- spend tracking ----------
    def month_spend(self, month: str | None = None) -> float:
        """Sum of actual (or estimated) cost of real-provider jobs this month."""
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        total = 0.0
        with self._lock:
            for j in self._data["jobs"]:
                if j.get("provider") == "mock":
                    continue
                if not str(j.get("createdAt", "")).startswith(month):
                    continue
                if j["status"] == "cancelled":
                    continue
                cost = j.get("actualCost")
                if cost is None:
                    cost = j.get("estimatedCost")
                total += float(cost or 0.0)
        return round(total, 2)
