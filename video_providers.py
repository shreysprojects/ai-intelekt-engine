"""Video provider adapters.

Architecture: every provider implements the same VideoProvider interface and
speaks in normalized requests/results, so the app is never coupled to one
vendor. Adapters:

  - MockProvider   — full local state machine, consumes no credits.
  - HeyGenProvider — PRIMARY. Built against the official v3 API reference
                     (developers.heygen.com, consulted 2026-07-16; see
                     VIDEO_PROVIDERS.md for exact sources).
  - TavusProvider  — FALLBACK. Built against docs.tavus.io v2 reference.

API keys come from environment variables only and never leave the server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

USER_AGENT = "ai-intelekt-engine/1.0"


class ProviderError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _result(provider_job_id="", status="queued", video_url="", thumbnail_url="",
            duration_seconds=None, estimated_cost=None, actual_cost=None,
            error_code="", error_message=""):
    return {
        "providerJobId": provider_job_id, "status": status, "videoUrl": video_url,
        "thumbnailUrl": thumbnail_url, "durationSeconds": duration_seconds,
        "estimatedCost": estimated_cost, "actualCost": actual_cost,
        "errorCode": error_code, "errorMessage": error_message,
    }


def _http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:  # noqa: BLE001
            pass
        retryable = e.code in (429, 500, 502, 503, 529)
        raise ProviderError(f"http_{e.code}", f"Provider returned HTTP {e.code}. {detail}", retryable) from e
    except urllib.error.URLError as e:
        raise ProviderError("network", f"Could not reach provider: {e.reason}", retryable=True) from e


class VideoProvider:
    """Interface every adapter implements."""

    name = "base"
    display_name = "Base provider"
    is_mock = False
    max_duration_seconds = 600
    supported_aspects = ("16:9", "9:16", "1:1")
    supports_reference_image = False
    supports_cancel = False
    supports_dialogue = False  # two-host podcast via per-speaker segments
    key_env_vars: tuple = ()   # env vars this provider reads its API key from

    def check_key(self, key: str):
        """Return (True, detail) if the key authenticates, (False, detail) if
        rejected, (None, detail) if the provider was unreachable."""
        return True, ""

    def validateConfiguration(self) -> dict:
        raise NotImplementedError

    def createVideo(self, request: dict) -> dict:
        raise NotImplementedError

    def getVideoStatus(self, provider_job_id: str) -> dict:
        raise NotImplementedError

    def cancelVideo(self, provider_job_id: str) -> dict:
        raise ProviderError("cancel_unsupported", f"{self.display_name} does not support cancelling a submitted video.")

    def getEstimatedCost(self, request: dict):
        return None

    def normalizeWebhook(self, headers: dict, raw_body: bytes) -> dict:
        raise ProviderError("webhook_unsupported", f"{self.display_name} webhooks are not supported.")

    def refreshOutputUrl(self, provider_job_id: str) -> dict:
        return self.getVideoStatus(provider_job_id)


# ======================================================================
# MOCK
# ======================================================================
class MockProvider(VideoProvider):
    """Simulates the full asynchronous lifecycle without spending credits.

    Timeline (from submission): 0-4s queued → 4-12s processing → completed.
    A narration containing "MOCK-FAIL" fails instead of completing.
    """

    name = "mock"
    display_name = "Mock Video Provider"
    is_mock = True
    max_duration_seconds = 600
    supports_reference_image = True
    supports_cancel = True
    supports_dialogue = True

    QUEUE_S = 4
    PROCESS_S = 12

    def __init__(self, now=time.time):
        self._now = now
        self._jobs: dict[str, dict] = {}

    def validateConfiguration(self) -> dict:
        return {"ok": True, "status": "ready", "detail": "Mock mode — no credits are used and no real video is produced."}

    def createVideo(self, request: dict) -> dict:
        job_id = "mock_" + hashlib.sha1(f"{request.get('jobId')}{self._now()}".encode()).hexdigest()[:10]
        self._jobs[job_id] = {
            "created": self._now(),
            "fail": "MOCK-FAIL" in (request.get("spokenScript") or ""),
            "cancelled": False,
            "seconds": request.get("estimatedSeconds") or 45,
        }
        return _result(provider_job_id=job_id, status="queued", estimated_cost=0.0)

    def getVideoStatus(self, provider_job_id: str) -> dict:
        j = self._jobs.get(provider_job_id)
        if not j:
            # Server restarted: mock jobs are memory-only. Treat as failed-stale.
            return _result(provider_job_id, "failed", error_code="mock_lost",
                           error_message="Mock job state was lost (server restart). Regenerate to run again.")
        if j["cancelled"]:
            return _result(provider_job_id, "cancelled", estimated_cost=0.0)
        age = self._now() - j["created"]
        if age < self.QUEUE_S:
            return _result(provider_job_id, "queued", estimated_cost=0.0)
        if age < self.QUEUE_S + self.PROCESS_S:
            return _result(provider_job_id, "processing", estimated_cost=0.0)
        if j["fail"]:
            return _result(provider_job_id, "failed", estimated_cost=0.0,
                           error_code="mock_forced_failure",
                           error_message="Simulated provider failure (narration contained MOCK-FAIL).")
        return _result(provider_job_id, "completed",
                       video_url="/mock-placeholder.svg", thumbnail_url="/mock-placeholder.svg",
                       duration_seconds=j["seconds"], estimated_cost=0.0, actual_cost=0.0)

    def cancelVideo(self, provider_job_id: str) -> dict:
        j = self._jobs.get(provider_job_id)
        if not j:
            raise ProviderError("not_found", "Unknown mock job.")
        status = self.getVideoStatus(provider_job_id)["status"]
        if status in ("completed", "failed"):
            raise ProviderError("terminal", f"Mock job is already {status}.")
        j["cancelled"] = True
        return _result(provider_job_id, "cancelled", estimated_cost=0.0)

    def getEstimatedCost(self, request: dict):
        return 0.0


# ======================================================================
# HEYGEN — PRIMARY
# Sources: developers.heygen.com/reference/create-video.md,
#          /reference/get-video.md, /docs/webhooks.md, /docs/pricing.md
# ======================================================================
class HeyGenProvider(VideoProvider):
    name = "heygen"
    display_name = "HeyGen"
    max_duration_seconds = 1800
    supported_aspects = ("16:9", "9:16", "1:1")
    supports_reference_image = True   # v3 "image" mode generates from a still image
    supports_cancel = False           # no cancel endpoint in the official v3 reference
    supports_dialogue = True          # podcast = one clip per speaker segment (v3 is single-avatar per video)
    key_env_vars = ("HEYGEN_API_KEY", "VIDEO_PROVIDER_API_KEY")

    def check_key(self, key: str):
        """Probe auth with a harmless GET (verified endpoint): 401/403 means a
        bad key; any other HTTP answer (e.g. 404 for a bogus id) means auth passed."""
        try:
            _http_json("GET", f"{self.base}/v3/videos/key-check-probe", {"x-api-key": key}, timeout=15)
            return True, "Key accepted by HeyGen."
        except ProviderError as e:
            if e.code in ("http_401", "http_403"):
                return False, "HeyGen rejected this key (authentication failed)."
            if e.code.startswith("http_"):
                return True, "Key accepted by HeyGen."
            return None, f"Could not reach HeyGen to verify the key ({e.message}). Saved anyway — it will be checked on first use."

    # Official API pricing (2026-07): photo avatar / image mode, Avatar IV.
    PHOTO_RATE_PER_SEC = 0.05
    TWIN_RATE_PER_SEC = 0.0667

    STATUS_MAP = {"pending": "queued", "waiting": "queued", "processing": "processing",
                  "completed": "completed", "failed": "failed"}

    def __init__(self, asset_loader=None):
        self.base = os.environ.get("VIDEO_PROVIDER_BASE_URL", "").strip() or "https://api.heygen.com"
        self._asset_loader = asset_loader  # callable(asset_id) -> (bytes, mime) | None

    def _key(self) -> str:
        key = os.environ.get("HEYGEN_API_KEY", "").strip() or os.environ.get("VIDEO_PROVIDER_API_KEY", "").strip()
        if not key:
            raise ProviderError("not_configured", "HeyGen API key is not set (HEYGEN_API_KEY or VIDEO_PROVIDER_API_KEY).")
        return key

    def _headers(self) -> dict:
        return {"x-api-key": self._key()}

    def validateConfiguration(self) -> dict:
        try:
            self._key()
        except ProviderError as e:
            return {"ok": False, "status": "not_configured", "detail": e.message}
        return {"ok": True, "status": "ready",
                "detail": "API key present. Avatar/voice IDs are validated per persona at generation time."}

    def createVideo(self, request: dict) -> dict:
        if request.get("segments"):
            return self._create_dialogue(request)
        body: dict = {
            "title": request.get("title") or "ai-InteleKt video",
            "voice_id": request["voiceId"],
            "script": request["spokenScript"],
            "resolution": request.get("resolution") or "1080p",
            "aspect_ratio": request.get("aspectRatio") or "16:9",
            "output_format": "mp4",
            "callback_id": request.get("jobId", ""),
        }
        if request.get("captions", {}).get("enabled", True):
            body["caption"] = {"file_format": "srt"}
        public_webhook = os.environ.get("VIDEO_WEBHOOK_PUBLIC_URL", "").strip()
        if public_webhook:
            body["callback_url"] = public_webhook.rstrip("/") + "/api/video/webhook/heygen"

        # An uploaded reference photo takes priority; a typed avatar ID is only
        # used when there is no photo (or its file can no longer be read).
        loaded = None
        if request.get("referenceAssetId") and self._asset_loader:
            loaded = self._asset_loader(request["referenceAssetId"])
        if loaded:
            content, mime = loaded
            body["type"] = "image"
            body["image"] = {"type": "base64", "media_type": mime,
                             "data": base64.b64encode(content).decode("ascii")}
            # image mode is Avatar IV implicitly; an explicit "engine" field is
            # only valid in avatar mode and gets a strict-validation 400 here
            body["expressiveness"] = request.get("expressiveness") or "medium"
        elif request.get("avatarId"):
            body["type"] = "avatar"
            body["avatar_id"] = request["avatarId"]
            # Photo-avatar-only extras (idle "listening" clips use these to keep
            # hands still). Only sent alongside motionPrompt so plain talk clips
            # keep the historical payload — digital twins reject both fields.
            if request.get("motionPrompt"):
                body["expressiveness"] = request.get("expressiveness") or "low"
        elif request.get("referenceAssetId"):
            raise ProviderError("missing_reference", "The persona's reference image file could not be read.")
        else:
            raise ProviderError("missing_reference", "No provider avatar or reference image configured for this persona.")

        if request.get("motionPrompt"):
            body["motion_prompt"] = request["motionPrompt"]

        headers = self._headers()
        if request.get("jobId"):
            headers["Idempotency-Key"] = request["jobId"][:255]

        try:
            resp = _http_json("POST", f"{self.base}/v3/videos", headers, body)
        except ProviderError as e:
            if "avatar_not_found" in e.message:
                raise ProviderError("avatar_not_found",
                                    "HeyGen has no avatar with the configured ID. In Settings, either enter a real avatar ID from your HeyGen account, or clear the avatar field and upload a reference photo instead.") from e
            if "voice_not_found" in e.message:
                raise ProviderError("voice_not_found",
                                    "HeyGen has no voice with the configured ID. In Settings, click 'Browse voices' to pick a real one from your account.") from e
            raise
        data = resp.get("data") or resp
        video_id = data.get("video_id")
        if not video_id:
            raise ProviderError("bad_response", f"HeyGen did not return a video_id: {str(resp)[:200]}")
        return _result(provider_job_id=video_id,
                       status=self.STATUS_MAP.get(str(data.get("status", "waiting")).lower(), "queued"),
                       estimated_cost=self.getEstimatedCost(request))

    def _create_dialogue(self, request: dict) -> dict:
        """Two-host podcast: HeyGen v3 renders one avatar per video, so each
        consecutive speaker block is submitted as its own clip. The clips are
        tracked as segments of a single job and downloaded in order for the
        podcast edit (two-shot stitching happens in the edit, not the API)."""
        segments_out = []
        for seg in request["segments"]:
            sub = dict(request)
            sub.update({
                "segments": None,
                "spokenScript": seg["text"],
                "voiceId": seg["voiceId"],
                "avatarId": seg.get("avatarId"),
                "referenceAssetId": seg.get("referenceAssetId"),
                "title": f"{request.get('title') or 'Podcast'} — part {seg['index'] + 1} ({seg['speaker']})",
                "jobId": f"{request.get('jobId', 'job')}-s{seg['index']}",
                "estimatedSeconds": max(4, round(len(seg['text'].split()) / 2.5)),
            })
            r = self.createVideo(sub)
            segments_out.append({"index": seg["index"], "speaker": seg["speaker"],
                                 "providerJobId": r["providerJobId"], "status": r["status"],
                                 "videoUrl": "", "thumbnailUrl": "", "durationSeconds": None,
                                 "errorMessage": ""})
        result = _result(provider_job_id=segments_out[0]["providerJobId"], status="queued",
                         estimated_cost=self.getEstimatedCost(request))
        result["segments"] = segments_out
        return result

    def list_voices(self) -> list:
        """GET /v3/voices (official reference) — private/cloned voices first."""
        resp = _http_json("GET", f"{self.base}/v3/voices?limit=100", self._headers())
        data = resp.get("data") or resp
        raw = data.get("voices") if isinstance(data, dict) else data
        if raw is None and isinstance(data, dict):
            raw = data.get("list") or []
        voices = []
        for v in raw or []:
            voices.append({"voiceId": v.get("voice_id", ""), "name": v.get("name", ""),
                           "language": v.get("language", ""), "gender": v.get("gender", ""),
                           "type": v.get("type", ""), "previewUrl": v.get("preview_audio_url") or ""})
        voices.sort(key=lambda v: (v["type"] != "private", v["name"].lower()))
        return voices

    def getVideoStatus(self, provider_job_id: str) -> dict:
        resp = _http_json("GET", f"{self.base}/v3/videos/{provider_job_id}", self._headers())
        data = resp.get("data") or resp
        raw = str(data.get("status", "")).lower()
        status = self.STATUS_MAP.get(raw, "processing")
        # Prefer the caption-burned output when available (presigned URLs — refresh by re-fetching).
        video_url = data.get("captioned_video_url") or data.get("video_url") or ""
        return _result(
            provider_job_id=provider_job_id, status=status,
            video_url=video_url if status == "completed" else "",
            thumbnail_url=data.get("thumbnail_url") or "",
            duration_seconds=data.get("duration"),
            error_code=data.get("failure_code") or "",
            error_message=data.get("failure_message") or "",
        )

    def getEstimatedCost(self, request: dict):
        seconds = request.get("estimatedSeconds") or 60
        # A reference photo (image mode) takes priority over an avatar ID.
        use_avatar = bool(request.get("avatarId")) and not request.get("referenceAssetId")
        rate = self.TWIN_RATE_PER_SEC if use_avatar else self.PHOTO_RATE_PER_SEC
        return round(seconds * rate, 2)

    def normalizeWebhook(self, headers: dict, raw_body: bytes) -> dict:
        """Verify HMAC-SHA256 over the raw body per the official webhook docs."""
        secret = os.environ.get("VIDEO_PROVIDER_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise ProviderError("webhook_not_configured", "VIDEO_PROVIDER_WEBHOOK_SECRET is not set.")
        lower = {k.lower(): v for k, v in headers.items()}
        signature = lower.get("heygen-signature", "")
        timestamp = lower.get("heygen-timestamp", "")
        event_id = lower.get("heygen-event-id", "")
        if not signature:
            raise ProviderError("webhook_bad_signature", "Missing Heygen-Signature header.")
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ProviderError("webhook_bad_signature", "Webhook signature verification failed.")
        try:
            if timestamp and abs(time.time() - float(timestamp)) > 300:
                raise ProviderError("webhook_stale", "Webhook timestamp outside the 5-minute window.")
        except ValueError:
            raise ProviderError("webhook_stale", "Webhook timestamp missing or malformed.") from None
        payload = json.loads(raw_body.decode("utf-8"))
        event_type = payload.get("event_type", "")
        detail = payload.get("event_data") or payload.get("data") or {}
        return {
            "eventId": event_id or hashlib.sha1(raw_body).hexdigest(),
            "eventType": event_type,
            "providerJobId": detail.get("video_id", ""),
            "callbackId": detail.get("callback_id", ""),
            "status": "completed" if event_type.endswith(".success") else
                      "failed" if event_type.endswith(".fail") else "",
            "videoUrl": detail.get("url") or detail.get("video_url") or "",
        }


# ======================================================================
# TAVUS — FALLBACK
# Sources: docs.tavus.io/api-reference/video-request/create-video, get-video
# ======================================================================
class TavusProvider(VideoProvider):
    name = "tavus"
    display_name = "Tavus"
    max_duration_seconds = 1800
    supported_aspects = ("16:9",)      # replica output; format controls are thinner than HeyGen's
    supports_reference_image = False   # requires a trained replica (consent footage), not a still image
    supports_cancel = False
    key_env_vars = ("TAVUS_API_KEY",)

    def check_key(self, key: str):
        try:
            _http_json("GET", f"{self.base}/v2/videos/key-check-probe", {"x-api-key": key}, timeout=15)
            return True, "Key accepted by Tavus."
        except ProviderError as e:
            if e.code in ("http_401", "http_403"):
                return False, "Tavus rejected this key (authentication failed)."
            if e.code.startswith("http_"):
                return True, "Key accepted by Tavus."
            return None, f"Could not reach Tavus to verify the key ({e.message}). Saved anyway — it will be checked on first use."

    STATUS_MAP = {"queued": "queued", "generating": "processing", "ready": "completed",
                  "error": "failed", "deleted": "cancelled"}

    def __init__(self, asset_loader=None):
        self.base = "https://tavusapi.com"

    def _key(self) -> str:
        key = os.environ.get("TAVUS_API_KEY", "").strip()
        if not key:
            raise ProviderError("not_configured", "Tavus API key is not set (TAVUS_API_KEY).")
        return key

    def validateConfiguration(self) -> dict:
        try:
            self._key()
        except ProviderError as e:
            return {"ok": False, "status": "not_configured", "detail": e.message}
        return {"ok": True, "status": "ready",
                "detail": "API key present. Each persona needs a trained Tavus replica ID (consent footage recorded on tavus.io)."}

    def createVideo(self, request: dict) -> dict:
        if request.get("segments"):
            raise ProviderError("dialogue_unsupported",
                                "Tavus support for the two-host podcast format is not implemented — use HeyGen (or the mock provider) for podcast episodes.")
        if not request.get("avatarId"):
            raise ProviderError("missing_reference",
                                "Tavus requires a trained replica ID for this persona (reference images are not enough — see Settings).")
        body = {
            "replica_id": request["avatarId"],
            "script": request["spokenScript"],
            "video_name": request.get("title") or "ai-InteleKt video",
        }
        public_webhook = os.environ.get("VIDEO_WEBHOOK_PUBLIC_URL", "").strip()
        if public_webhook:
            body["callback_url"] = public_webhook.rstrip("/") + "/api/video/webhook/tavus"
        resp = _http_json("POST", f"{self.base}/v2/videos", {"x-api-key": self._key()}, body)
        video_id = resp.get("video_id")
        if not video_id:
            raise ProviderError("bad_response", f"Tavus did not return a video_id: {str(resp)[:200]}")
        return _result(provider_job_id=video_id,
                       status=self.STATUS_MAP.get(str(resp.get("status", "queued")).lower(), "queued"))

    def getVideoStatus(self, provider_job_id: str) -> dict:
        resp = _http_json("GET", f"{self.base}/v2/videos/{provider_job_id}?verbose=true",
                          {"x-api-key": self._key()})
        raw = str(resp.get("status", "")).lower()
        status = self.STATUS_MAP.get(raw, "processing")
        return _result(
            provider_job_id=provider_job_id, status=status,
            video_url=(resp.get("download_url") or resp.get("stream_url") or resp.get("hosted_url") or "")
            if status == "completed" else "",
            thumbnail_url=resp.get("still_image_thumbnail_url") or "",
            error_message=str(resp.get("error", "") or ""),
        )

    def getEstimatedCost(self, request: dict):
        return None  # Tavus does not publish a per-second API price we can compute from; shown as "unknown".

    def normalizeWebhook(self, headers: dict, raw_body: bytes) -> dict:
        """Tavus docs do not document a webhook signature, so the payload is
        treated as an untrusted hint: we extract the video_id and RE-POLL the
        API for the real status rather than trusting the body."""
        payload = json.loads(raw_body.decode("utf-8"))
        video_id = payload.get("video_id", "")
        if not video_id:
            raise ProviderError("webhook_bad_payload", "Tavus webhook had no video_id.")
        return {"eventId": hashlib.sha1(raw_body).hexdigest(), "eventType": "tavus.callback",
                "providerJobId": video_id, "callbackId": "", "status": "", "videoUrl": "",
                "requiresRepoll": True}


def build_providers(asset_loader=None, now=time.time) -> dict:
    return {
        "mock": MockProvider(now=now),
        "heygen": HeyGenProvider(asset_loader=asset_loader),
        "tavus": TavusProvider(asset_loader=asset_loader),
    }
