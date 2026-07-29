"""ai-InteleKt Content Engine — local web server (Python).

Run with:  py app.py   → open http://localhost:3000

The browser page is display-only; pipeline logic lives in pipeline.py and the
video-production workflow in video_service.py / video_providers.py.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None):
    """Tiny .env loader (KEY=VALUE lines; existing env vars win)."""
    p = path or (BASE_DIR / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()  # must run before modules read env defaults

import pipeline  # noqa: E402
import video_prep  # noqa: E402  (imported for side-free helpers used in handlers)
from video_providers import ProviderError, build_providers  # noqa: E402
from video_service import VideoService  # noqa: E402
from video_store import VideoStore  # noqa: E402

PORT = 3000
PUBLIC_DIR = BASE_DIR / "public"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MIN_IMAGE_DIM = 256

_lock = threading.Lock()

store = VideoStore()
providers = build_providers(asset_loader=store.asset_bytes)
video = VideoService(store, providers)


# ----------------------------------------------------------------------
# Pipeline-run state (unchanged behaviour from before)
# ----------------------------------------------------------------------
def _fresh_stages():
    return [
        {"key": key, "name": name, "status": "pending", "seconds": 0,
         "searches": 0, "output": None, "note": ""}
        for key, name, _tools, _effort in pipeline.STAGES
    ]


STATE = {
    "running": False, "model": None, "stages": _fresh_stages(),
    "pack": None, "usage": None, "cost": None, "saved": None, "error": None,
}
_stage_started_at: dict[str, float] = {}


def _find(key):
    for s in STATE["stages"]:
        if s["key"] == key:
            return s
    return None


def _on_progress(event, key, data):
    with _lock:
        s = _find(key)
        if not s:
            return
        if event == "stage_start":
            s["status"] = "running"
            _stage_started_at[key] = time.time()
        elif event == "stage_done":
            s["status"] = "done"
            s["seconds"] = data.get("seconds", 0)
            s["searches"] = data.get("searches", 0)
            s["output"] = data.get("output")
            errs = data.get("tool_errors") or []
            if errs:
                s["note"] = "tool errors: " + ", ".join(errs)
        elif event == "stage_error":
            s["status"] = "error"
            s["note"] = data.get("error", "failed")


def _run_thread(api_key, model):
    try:
        result = pipeline.run_pipeline(api_key, model, _on_progress)
        with _lock:
            STATE.update(pack=result["pack"], usage=result["usage"],
                         cost=result["cost"], saved=result["saved"])
    except Exception as e:  # noqa: BLE001 — surfaced to the UI
        with _lock:
            STATE["error"] = str(e)
    finally:
        with _lock:
            STATE["running"] = False


# ----------------------------------------------------------------------
# Provider API keys — saved server-side into .env, never echoed back
# ----------------------------------------------------------------------
ENV_PATH = BASE_DIR / ".env"
PROVIDER_KEY_VARS = {"heygen": "HEYGEN_API_KEY", "tavus": "TAVUS_API_KEY"}


def update_env_file(name: str, value: str, path: Path | None = None):
    """Set (or remove, when value is empty) NAME=value in the .env file,
    preserving every other line. The file itself is git-ignored."""
    p = path or ENV_PATH
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    out, replaced = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == name:
            if value and not replaced:
                out.append(f"{name}={value}")
                replaced = True
            continue  # drop cleared keys and duplicates
        out.append(line)
    if value and not replaced:
        out.append(f"{name}={value}")
    p.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def save_provider_key(provider_name: str, key: str) -> dict:
    var = PROVIDER_KEY_VARS.get(provider_name)
    if not var:
        return {"error": f"Unknown provider: {provider_name}"}
    key = (key or "").strip()
    if key and (len(key) < 8 or any(c.isspace() for c in key)):
        return {"error": "That doesn't look like a valid API key (too short or contains spaces)."}
    warning = None
    if key:
        p = providers.get(provider_name)
        ok, detail = p.check_key(key)
        if ok is False:
            return {"error": detail}
        if ok is None:
            warning = detail
        os.environ[var] = key
    else:
        os.environ.pop(var, None)
    update_env_file(var, key)
    return {"configured": bool(key), "hint": ("••••" + key[-4:]) if key else "",
            "warning": warning, "provider": provider_name}


# ----------------------------------------------------------------------
# Image validation (magic bytes + dimensions, stdlib only)
# ----------------------------------------------------------------------
def image_info(content: bytes) -> tuple[str, int, int] | None:
    """Return (mime, width, height) for PNG/JPEG content, else None."""
    if content[:8] == b"\x89PNG\r\n\x1a\n" and len(content) > 24:
        w, h = struct.unpack(">II", content[16:24])
        return "image/png", w, h
    if content[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(content):
            if content[i] != 0xFF:
                i += 1
                continue
            marker = content[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", content[i + 5:i + 9])
                return "image/jpeg", w, h
            seg_len = struct.unpack(">H", content[i + 2:i + 4])[0]
            i += 2 + seg_len
        return "image/jpeg", 0, 0
    return None


# ----------------------------------------------------------------------
# Background provider polling
# ----------------------------------------------------------------------
def _poll_loop():
    interval = max(5, int(os.environ.get("VIDEO_PROVIDER_POLL_INTERVAL_SECONDS", "15") or 15))
    backoff = interval
    while True:
        try:
            if store.active_jobs():
                video.poll_active_jobs()
                backoff = interval
            time.sleep(backoff)
        except Exception as e:  # noqa: BLE001 — polling must never die
            print(f"[poll] error: {e}")
            backoff = min(backoff * 2, 300)
            time.sleep(backoff)


# ----------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console for pipeline logs only
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, file: Path, mime: str):
        """Serve a local file with HTTP Range support (needed for <video> seek)."""
        size = file.stat().st_size
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = False
        if rng.startswith("bytes="):
            partial = True
            first, _, last = rng[len("bytes="):].partition("-")
            try:
                start = int(first) if first else 0
                end = int(last) if last else size - 1
            except ValueError:
                start, end = 0, size - 1
            start, end = max(0, start), min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(file, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return None

    def _read_body_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD_BYTES * 2:
            raise ValueError("Body too large")
        return self.rfile.read(length) if length else b""

    def _read_body(self) -> dict:
        raw = self._read_body_raw()
        return json.loads(raw or b"{}")

    # ------------------------------ GET ------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/status":
                with _lock:
                    snap = copy.deepcopy(STATE)
                for s in snap["stages"]:
                    if s["status"] == "running" and s["key"] in _stage_started_at:
                        s["seconds"] = round(time.time() - _stage_started_at[s["key"]])
                return self._json(200, snap)

            if path == "/api/runs":
                return self._json(200, {"runs": video.list_runs()})

            if path.startswith("/api/run/"):
                run = video.get_run(path.split("/api/run/", 1)[1])
                return self._json(200, run) if run else self._json(404, {"error": "Run not found"})

            if path == "/api/video/state":
                return self._json(200, video.state())

            if path == "/api/video/provider-voices":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                prov_name = (q.get("provider") or ["heygen"])[0]
                p = providers.get(prov_name)
                if not p or not hasattr(p, "list_voices"):
                    return self._json(400, {"error": f"{prov_name} does not support voice listing."})
                try:
                    return self._json(200, {"voices": p.list_voices()})
                except ProviderError as e:
                    return self._json(400, {"error": e.message})

            if path.startswith("/api/video/job/"):
                job_id = path.split("/api/video/job/", 1)[1]
                job = store.get_job(job_id)
                if not job:
                    return self._json(404, {"error": "Job not found"})
                approval = store.get_approval(job["approvalId"])
                return self._json(200, {"job": job, "events": store.list_events(job_id),
                                        "approval": approval})

            if path.startswith("/api/video/asset/"):
                loaded = store.asset_bytes(path.split("/api/video/asset/", 1)[1])
                if not loaded:
                    return self._json(404, {"error": "Asset not found"})
                content, mime = loaded
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return None

            if path.startswith("/api/video/two-shot/"):
                job_id = path.split("/api/video/two-shot/", 1)[1]
                job = store.get_job(job_id)
                ts = (job or {}).get("twoShot") or {}
                fpath = Path(ts.get("path") or "")
                if not job or ts.get("status") != "ready" or not fpath.is_file():
                    return self._json(404, {"error": "Two-shot not available for this job."})
                return self._serve_file(fpath, "video/mp4")

            # static files
            rel = "index.html" if path == "/" else path.lstrip("/")
            file = (PUBLIC_DIR / rel).resolve()
            if not str(file).startswith(str(PUBLIC_DIR)) or not file.is_file():
                return self._json(404, {"error": "Not found"})
            mime = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                    "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}.get(
                        file.suffix.lstrip("."), "application/octet-stream")
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            # Dashboard files change with the app — never let the browser serve stale JS/HTML.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return None
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 — never crash the handler thread
            return self._json(500, {"error": str(e)})

    # ------------------------------ POST ------------------------------
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path.startswith("/api/video/webhook/"):
                provider_name = path.split("/api/video/webhook/", 1)[1]
                raw = self._read_body_raw()
                try:
                    result = video.handle_webhook(provider_name, dict(self.headers), raw)
                    return self._json(200, result)
                except ProviderError as e:
                    code = 401 if "signature" in e.code or "stale" in e.code else 400
                    return self._json(code, {"error": e.message})

            body = self._read_body()

            if path == "/api/run":
                return self._start_pipeline(body)

            if path == "/api/video/approve":
                result = video.approve(body.get("runId", ""), body.get("scriptKey", ""),
                                       body.get("approvedBy", ""), body.get("overrides") or {})
                return self._json(400 if result.get("errors") else 200, result)

            if path == "/api/video/reject":
                result = video.reject(body.get("runId", ""), body.get("scriptKey", ""),
                                      body.get("note", ""), body.get("rejectedBy", ""))
                return self._json(400 if result.get("errors") else 200, result)

            if path == "/api/video/generate":
                result = video.generate(body.get("approvalId", ""), body)
                return self._json(400 if result.get("errors") else 200, result)

            if path == "/api/video/two-shot-rebuild":
                result = video.rebuild_two_shot(body.get("jobId", ""))
                return self._json(400 if result.get("errors") else 200, result)

            if path == "/api/video/cancel":
                result = video.cancel(body.get("jobId", ""))
                return self._json(400 if result.get("errors") else 200, result)

            if path == "/api/video/refresh-url":
                result = video.refresh_output(body.get("jobId", ""))
                return self._json(400 if result.get("errors") else 200, result)

            if path == "/api/video/settings":
                settings = store.update_settings(body or {})
                return self._json(200, {"settings": settings})

            if path == "/api/video/provider-key":
                result = save_provider_key(body.get("provider", ""), body.get("apiKey", ""))
                return self._json(400 if result.get("error") else 200, result)

            if path == "/api/video/asset":
                return self._upload_asset(body)

            return self._json(404, {"error": "Not found"})
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": f"Invalid request: {e}"})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": str(e)})

    def _start_pipeline(self, body: dict):
        api_key = (body.get("apiKey") or "").strip()
        model = body.get("model") or "claude-opus-4-8"
        if not api_key:
            return self._json(400, {"error": "Missing API key. Paste your Anthropic API key at the top of the page."})
        with _lock:
            if STATE["running"]:
                return self._json(409, {"error": "A run is already in progress."})
            STATE.update(running=True, model=model, stages=_fresh_stages(),
                         pack=None, usage=None, cost=None, saved=None, error=None)
            _stage_started_at.clear()
        threading.Thread(target=_run_thread, args=(api_key, model), daemon=True).start()
        return self._json(200, {"started": True})

    def _upload_asset(self, body: dict):
        persona = body.get("persona", "")
        if persona not in ("Ravi", "Rik", "Product"):
            return self._json(400, {"error": "persona must be Ravi, Rik, or Product."})
        try:
            content = base64.b64decode(body.get("dataBase64", ""), validate=True)
        except Exception:  # noqa: BLE001
            return self._json(400, {"error": "dataBase64 is not valid base64."})
        if not content:
            return self._json(400, {"error": "Empty file."})
        if len(content) > MAX_UPLOAD_BYTES:
            return self._json(400, {"error": f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)}MB upload limit."})
        info = image_info(content)
        if not info:
            return self._json(400, {"error": "Only PNG or JPEG images are accepted (content check failed, not just the filename)."})
        mime, w, h = info
        if w and h and (w < MIN_IMAGE_DIM or h < MIN_IMAGE_DIM):
            return self._json(400, {"error": f"Image is too small ({w}×{h}); providers need at least {MIN_IMAGE_DIM}px on each side."})
        asset = store.add_asset(persona, body.get("filename", "upload"), content, mime, w or None, h or None)
        store.update_settings({"personas": {persona: {"referenceAssetId": asset["id"]}}})
        return self._json(200, {"asset": asset})


if __name__ == "__main__":
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ai-InteleKt Content Engine (Python) running at http://localhost:{PORT}")
    print(f"Runs are saved to: {pipeline.RUNS_DIR}")
    print(f"Video provider: {store.get_settings()['provider']}"
          + (" (MOCK — no credits used)" if store.get_settings()["provider"] == "mock" else ""))
    server.serve_forever()
