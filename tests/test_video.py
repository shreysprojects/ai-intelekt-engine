"""Tests for the approval → video-generation workflow.

Run:  py -m unittest discover -s tests -v
Uses the mock provider and temp storage only — no network, no credits.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline  # noqa: E402
import video_prep  # noqa: E402
from video_providers import (HeyGenProvider, MockProvider, ProviderError,  # noqa: E402
                             VideoProvider, _result)
from video_service import VideoService  # noqa: E402
from video_store import VideoStore  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
def script_a_obj():
    return {
        "pipeline": "A", "voice": "product",
        "title_working": "Loyalty is measured value", "runtime_seconds": 45,
        "beats": [
            {"beat": "HOOK", "vo": "Your best customers stay because the value adds up.",
             "on_screen_text": "Value adds up", "visual_direction": "Push-in on POS [INFERRED]"},
            {"beat": "PROOF", "vo": "90% of consumers say a loyalty program makes them buy again.",
             "on_screen_text": "90% buy again", "visual_direction": "Stat card"},
            {"beat": "CTA", "vo": "If you're rethinking retention, take a look.",
             "on_screen_text": "Comment CLV", "visual_direction": "Logo end card"},
        ],
        "facts_used": [{"fact": "90% of consumers say a loyalty program makes them buy from that brand again",
                        "source": "CX Dive / LoyaltyLion"}],
        "source_story_url": "https://example.com/story",
    }


def synopsis_a_obj():
    return {
        "pipeline": "A",
        "approved_facts": [{"fact": "90% of consumers say belonging to a loyalty program makes them purchase again",
                            "source": "CX Dive", "url": "https://example.com/story", "verified": True}],
        "do_not_use": [],
    }


def script_b_obj():
    return {
        "pipeline": "B", "voice": "Rik",
        "title_working": "The acquisition trap", "runtime_seconds": 50,
        "beats": [
            {"beat": "HOOK", "vo": "Growth you cannot prove per customer is a treadmill.",
             "on_screen_text": "Treadmill", "visual_direction": "Direct to camera"},
            {"beat": "CLOSE", "vo": "Which kind are you building?",
             "on_screen_text": "Which kind?", "visual_direction": "Hold two beats"},
        ],
        "facts_used": [],
        "source_story_url": "",
    }


def synopsis_b_obj():
    return {"pipeline": "B", "approved_facts": [], "do_not_use": []}


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def advance(self, s):
        self.t += s


class StubRealProvider(VideoProvider):
    """A non-mock provider for cost-limit and error-normalization tests."""
    name = "stub"
    display_name = "Stub Real Provider"
    is_mock = False
    supports_reference_image = True

    def __init__(self, cost=50.0, fail_code=None):
        self.cost = cost
        self.fail_code = fail_code

    def validateConfiguration(self):
        return {"ok": True, "status": "ready", "detail": "stub"}

    def createVideo(self, request):
        if self.fail_code:
            raise ProviderError(self.fail_code, "Simulated provider rejection.")
        return _result(provider_job_id="stub-1", status="queued", estimated_cost=self.cost)

    def getVideoStatus(self, provider_job_id):
        return _result(provider_job_id, "processing")

    def getEstimatedCost(self, request):
        return self.cost


class VideoWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs = root / "runs"
        self.run_id = "2026-07-16_test"
        run_dir = self.runs / self.run_id
        run_dir.mkdir(parents=True)
        stages = {
            "scriptA": json.dumps(script_a_obj()),
            "synopsisA": json.dumps(synopsis_a_obj()),
            "scriptB": json.dumps(script_b_obj()),
            "synopsisB": json.dumps(synopsis_b_obj()),
        }
        (run_dir / "stages.json").write_text(json.dumps(stages), encoding="utf-8")
        (run_dir / "handoff-pack.md").write_text("# pack", encoding="utf-8")

        self.clock = FakeClock()
        self.store = VideoStore(path=root / "store.json", assets_dir=root / "assets")
        self.mock = MockProvider(now=self.clock.time)
        self.providers = {"mock": self.mock,
                          "stub": StubRealProvider(),
                          "heygen": HeyGenProvider(asset_loader=self.store.asset_bytes)}
        self.svc = VideoService(self.store, self.providers, runs_dir=self.runs, now=self.clock.time)
        # Configure personas (names must not look like mock placeholders —
        # the real-provider guard rejects anything starting with "mock")
        self.store.update_settings({"personas": {
            "Product": {"providerAvatarId": "test-avatar", "voiceId": "test-voice"},
            "Rik": {"providerAvatarId": "test-avatar-rik", "voiceId": "test-voice-rik"},
            "Ravi": {"providerAvatarId": "test-avatar-ravi", "voiceId": "test-voice-ravi"},
        }})

    def tearDown(self):
        self.tmp.cleanup()

    # -------- helpers --------
    def approve_a(self):
        r = self.svc.approve(self.run_id, "scriptA", approved_by="tester")
        self.assertNotIn("errors", r, r.get("errors"))
        return r["approval"]

    def generate(self, approval_id, **kw):
        opts = {"confirm": True, "idempotencyKey": kw.pop("idem", "key-1")}
        opts.update(kw)
        return self.svc.generate(approval_id, opts)

    def edit_script_a(self, mutate):
        p = self.runs / self.run_id / "stages.json"
        stages = json.loads(p.read_text(encoding="utf-8"))
        obj = json.loads(stages["scriptA"])
        mutate(obj)
        stages["scriptA"] = json.dumps(obj)
        p.write_text(json.dumps(stages), encoding="utf-8")

    # 1. existing content generation is untouched
    def test_pipeline_module_intact(self):
        self.assertEqual(len(pipeline.STAGES), 7)
        self.assertIn("FOUNDATION BLOCK", pipeline.FOUNDATION)
        self.assertTrue(callable(pipeline.run_pipeline))

    # 2. approved script can be submitted
    def test_approved_script_generates(self):
        approval = self.approve_a()
        r = self.generate(approval["id"])
        self.assertNotIn("errors", r, r.get("errors"))
        self.assertEqual(r["job"]["status"], "queued")

    # 3. unapproved script cannot be submitted
    def test_unapproved_cannot_generate(self):
        r = self.svc.generate("apv_nonexistent", {"confirm": True})
        self.assertIn("errors", r)
        rejected = self.svc.reject(self.run_id, "scriptA", note="not today")
        r2 = self.generate(rejected["approval"]["id"])
        self.assertTrue(any("not approved" in e for e in r2["errors"]))

    # 4. editing after approval invalidates it
    def test_edit_invalidates_approval(self):
        approval = self.approve_a()
        self.edit_script_a(lambda o: o["beats"].append(
            {"beat": "CTA", "vo": "New line added later.", "on_screen_text": "", "visual_direction": ""}))
        r = self.generate(approval["id"])
        self.assertTrue(any("changed after approval" in e for e in r["errors"]))
        self.assertEqual(self.store.get_approval(approval["id"])["status"], "awaiting_review")

    # 5. duplicate click cannot create duplicate paid jobs
    def test_idempotent_generation(self):
        approval = self.approve_a()
        r1 = self.generate(approval["id"], idem="same-key")
        r2 = self.generate(approval["id"], idem="same-key")
        self.assertEqual(r1["job"]["id"], r2["job"]["id"])
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(len(self.store.list_jobs(approval["id"])), 1)

    # 6-8. persona routing
    def test_persona_routing(self):
        self.assertEqual(video_prep.persona_for_voice("Ravi"), "Ravi")
        self.assertEqual(video_prep.persona_for_voice("rik"), "Rik")
        self.assertEqual(video_prep.persona_for_voice("product"), "Product")
        a = self.approve_a()
        self.assertEqual(a["persona"], "Product")
        b = self.svc.approve(self.run_id, "scriptB")["approval"]
        self.assertEqual(b["persona"], "Rik")

    # 9-10. default reference asset + voice selection
    def test_persona_config_snapshot(self):
        a = self.svc.approve(self.run_id, "scriptB")["approval"]
        self.assertEqual(a["personaConfigSnapshot"]["providerAvatarId"], "test-avatar-rik")
        self.assertEqual(a["personaConfigSnapshot"]["voiceId"], "test-voice-rik")

    # 11-12. narration hygiene
    def test_narration_excludes_nonspoken_content(self):
        a = self.approve_a()
        narration = a["approvedScriptSnapshot"]["narration"]
        self.assertNotIn("http", narration)
        self.assertNotIn("[INFERRED]", narration)
        self.assertNotIn("HOOK", narration)
        self.assertNotIn("Push-in", narration)      # visual direction
        self.assertNotIn("Stat card", narration)
        self.assertIn("90%", narration)              # approved stat preserved

    # 13. [VERIFY] blocks approval
    def test_verify_marker_blocks(self):
        self.edit_script_a(lambda o: o["beats"].__setitem__(0, {
            "beat": "HOOK", "vo": "Value adds up [VERIFY: check me].",
            "on_screen_text": "", "visual_direction": ""}))
        r = self.svc.approve(self.run_id, "scriptA")
        self.assertTrue(any("[VERIFY]" in e or "VERIFY" in e for e in r["errors"]))

    # 14. DO NOT USE blocks approval
    def test_do_not_use_blocks(self):
        self.edit_script_a(lambda o: o["beats"].__setitem__(0, {
            "beat": "HOOK", "vo": "DO NOT USE this claim on air.",
            "on_screen_text": "", "visual_direction": ""}))
        r = self.svc.approve(self.run_id, "scriptA")
        self.assertTrue(any("DO NOT USE" in e for e in r["errors"]))

    # unsupported statistics block approval
    def test_unsupported_stat_blocks(self):
        self.edit_script_a(lambda o: o["beats"].append({
            "beat": "PROOF", "vo": "Retailers save $4 billion with this.",
            "on_screen_text": "", "visual_direction": ""}))
        r = self.svc.approve(self.run_id, "scriptA")
        self.assertTrue(any("statistic" in e for e in r["errors"]))

    # unverified required source blocks approval
    def test_unverified_source_blocks(self):
        p = self.runs / self.run_id / "stages.json"
        stages = json.loads(p.read_text(encoding="utf-8"))
        syn = json.loads(stages["synopsisA"])
        syn["approved_facts"][0]["verified"] = False
        stages["synopsisA"] = json.dumps(syn)
        p.write_text(json.dumps(stages), encoding="utf-8")
        r = self.svc.approve(self.run_id, "scriptA")
        self.assertTrue(any("unverified" in e for e in r["errors"]))

    # 15-16. missing image / voice block submission
    def test_missing_reference_blocks(self):
        self.store.update_settings({"personas": {"Product": {"providerAvatarId": None, "voiceId": "v"}}})
        a = self.approve_a()
        r = self.generate(a["id"])
        self.assertTrue(any("reference image" in e or "avatar" in e for e in r["errors"]))

    def test_missing_voice_auto_picks_random(self):
        """No voice set no longer blocks: a random provider voice is picked and
        saved to Settings (the mock provider gets an in-memory stand-in)."""
        self.store.update_settings({"personas": {"Product": {"providerAvatarId": "x", "voiceId": None}}})
        a = self.approve_a()
        r = self.generate(a["id"])  # mock provider
        self.assertNotIn("errors", r, r.get("errors"))
        # the mock stand-in voice is never persisted to Settings
        self.assertIsNone(self.store.get_settings()["personas"]["Product"]["voiceId"])
        self.clock.advance(30)
        self.svc.poll_active_jobs()  # finish the mock job so a new attempt can start

        stub = StubRealProvider()
        stub.list_voices = lambda: [
            {"voiceId": "v-en", "name": "A", "language": "English", "gender": "", "type": "public"},
            {"voiceId": "v-fr", "name": "B", "language": "French", "gender": "", "type": "public"},
        ]
        self.providers["stub"] = stub
        r2 = self.generate(a["id"], provider="stub", idem="key-2")
        self.assertNotIn("errors", r2, r2.get("errors"))
        # the pick (English preferred) is saved so later generations reuse it
        self.assertEqual(self.store.get_settings()["personas"]["Product"]["voiceId"], "v-en")

    def test_missing_voice_still_blocks_without_voice_catalog(self):
        self.store.update_settings({"personas": {"Product": {"providerAvatarId": "x", "voiceId": None}}})
        a = self.approve_a()
        r = self.generate(a["id"], provider="stub")  # stub has no list_voices
        self.assertTrue(any("voice" in e.lower() for e in r["errors"]))

    def test_photo_takes_priority_over_avatar_id(self):
        """An uploaded reference photo wins over a (possibly stale) avatar ID."""
        asset = self.store.add_asset("Product", "face.png", b"png", "image/png", 512, 512)
        self.store.update_settings({"personas": {"Product": {
            "referenceAssetId": asset["id"], "providerAvatarId": "mock-avatar", "voiceId": "real-voice"}}})
        a = self.approve_a()
        r = self.generate(a["id"], provider="stub")  # placeholder avatar ignored — photo wins
        self.assertNotIn("errors", r, r.get("errors"))
        hey = self.providers["heygen"]
        req = {"estimatedSeconds": 100, "avatarId": "av", "referenceAssetId": asset["id"]}
        self.assertEqual(hey.getEstimatedCost(req), 100 * hey.PHOTO_RATE_PER_SEC)

    # 17. mock completes
    def test_mock_completes(self):
        a = self.approve_a()
        job = self.generate(a["id"])["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        job = self.store.get_job(job["id"])
        self.assertEqual(job["status"], "completed")
        self.assertIn("mock-placeholder", job["videoUrl"])
        self.assertEqual(self.store.get_approval(a["id"])["status"], "completed")

    # 18. mock can fail
    def test_mock_failure(self):
        self.edit_script_a(lambda o: o["beats"].append({
            "beat": "CTA", "vo": "And now this run will MOCK-FAIL on purpose.",
            "on_screen_text": "", "visual_direction": ""}))
        a = self.approve_a()
        job = self.generate(a["id"])["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        job = self.store.get_job(job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.store.get_approval(a["id"])["status"], "generation_failed")

    # 19. provider errors normalize safely
    def test_provider_error_normalized(self):
        self.providers["stub"] = StubRealProvider(fail_code="insufficient_credits")
        a = self.approve_a()
        r = self.generate(a["id"], provider="stub")
        self.assertTrue(any("Simulated provider rejection" in e for e in r["errors"]))
        jobs = self.store.list_jobs(a["id"])
        self.assertEqual(jobs[-1]["status"], "failed")
        self.assertEqual(jobs[-1]["errorCode"], "insufficient_credits")
        # original run untouched
        self.assertTrue((self.runs / self.run_id / "handoff-pack.md").exists())

    # 20-21. webhooks: idempotent + signature verified
    def test_webhook_signature_and_idempotency(self):
        os.environ["VIDEO_PROVIDER_WEBHOOK_SECRET"] = "testsecret"
        try:
            hg = self.providers["heygen"]
            body = json.dumps({"event_type": "avatar_video.success",
                               "event_data": {"video_id": "v123", "url": "https://x/video.mp4"}}).encode()
            sig = hmac.new(b"testsecret", body, hashlib.sha256).hexdigest()
            headers = {"Heygen-Signature": sig, "Heygen-Timestamp": str(time.time()),
                       "Heygen-Event-Id": "evt-1"}
            event = hg.normalizeWebhook(headers, body)
            self.assertEqual(event["status"], "completed")
            with self.assertRaises(ProviderError):
                hg.normalizeWebhook({**headers, "Heygen-Signature": "bad" * 20}, body)
            self.assertFalse(self.store.webhook_seen("heygen", "evt-1"))
            self.assertTrue(self.store.webhook_seen("heygen", "evt-1"))  # duplicate
        finally:
            del os.environ["VIDEO_PROVIDER_WEBHOOK_SECRET"]

    # 22-23. regeneration = new attempt, old attempts retained
    def test_regenerate_keeps_history(self):
        a = self.approve_a()
        j1 = self.generate(a["id"], idem="k1")["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        j2 = self.generate(a["id"], idem="k2")["job"]
        self.assertEqual(j2["attemptNumber"], 2)
        jobs = self.store.list_jobs(a["id"])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], j1["id"])
        self.assertEqual(self.store.get_job(j1["id"])["status"], "completed")

    # 24. API keys never reach the client
    def test_no_secrets_in_state(self):
        os.environ["HEYGEN_API_KEY"] = "sk-secret-heygen-key-123"
        try:
            state_json = json.dumps(self.svc.state())
            self.assertNotIn("sk-secret-heygen-key-123", state_json)
        finally:
            del os.environ["HEYGEN_API_KEY"]

    # provider keys: .env writer preserves other content; state shows only a masked hint
    def test_env_file_writer(self):
        import app
        p = Path(self.tmp.name) / "envtest.env"
        p.write_text("# comment\nOTHER=keep\nHEYGEN_API_KEY=old\n", encoding="utf-8")
        app.update_env_file("HEYGEN_API_KEY", "newvalue123", path=p)
        text = p.read_text(encoding="utf-8")
        self.assertIn("HEYGEN_API_KEY=newvalue123", text)
        self.assertIn("OTHER=keep", text)
        self.assertIn("# comment", text)
        self.assertNotIn("old", text)
        app.update_env_file("TAVUS_API_KEY", "tavuskey9999", path=p)
        self.assertIn("TAVUS_API_KEY=tavuskey9999", p.read_text(encoding="utf-8"))
        app.update_env_file("HEYGEN_API_KEY", "", path=p)
        self.assertNotIn("HEYGEN_API_KEY", p.read_text(encoding="utf-8"))

    def test_state_masks_provider_keys(self):
        os.environ["HEYGEN_API_KEY"] = "super-secret-key-abcd"
        try:
            state = self.svc.state()
            self.assertTrue(state["providerStatus"]["heygen"]["keyConfigured"])
            self.assertEqual(state["providerStatus"]["heygen"]["keyHint"], "••••abcd")
            self.assertNotIn("super-secret-key-abcd", json.dumps(state))
        finally:
            del os.environ["HEYGEN_API_KEY"]

    # 25. unsupported uploads rejected
    def test_image_validation(self):
        import app  # noqa: PLC0415 — imports the handler helpers
        self.assertIsNone(app.image_info(b"this is not an image at all"))
        png_1x1 = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" +
                   struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)
        mime, w, h = app.image_info(png_1x1)
        self.assertEqual((mime, w, h), ("image/png", 1, 1))  # then rejected by MIN_IMAGE_DIM in the handler

    # 26. monthly hard limit blocks real providers
    def test_monthly_limit_blocks_real(self):
        self.store.update_settings({"monthlyHardLimit": 10})
        a = self.approve_a()
        r = self.generate(a["id"], provider="stub")  # stub estimates $50
        self.assertTrue(any("spending limit" in e for e in r["errors"]))

    # 27. mock is exempt from the paid limit
    def test_mock_exempt_from_limit(self):
        self.store.update_settings({"monthlyHardLimit": 0.01})
        a = self.approve_a()
        r = self.generate(a["id"])  # mock
        self.assertNotIn("errors", r, r.get("errors"))

    # -------- scout freshness flag --------
    def test_freshness_flag_prefers_model_note(self):
        out = json.dumps({"pipeline": "A", "date": "2026-08-03",
                          "freshness_note": "No 48-hour primary news found; freshest coverage is from July.",
                          "candidates": [{"published": "2026-08-03", "confidence": "HIGH"}]})
        self.assertIn("No 48-hour primary news", pipeline.freshness_flag(out))

    def test_freshness_flag_stale_dates_heuristic(self):
        # mirrors the 2026-08-03 real run: no note field, HIGH pick dated July 1
        out = json.dumps({"pipeline": "A", "date": "2026-08-03", "candidates": [
            {"published": "2026-07-01", "confidence": "HIGH"},
            {"published": "2026-07-27", "confidence": "MEDIUM"}]})
        flag = pipeline.freshness_flag(out)
        self.assertIn("last 48 hours", flag)
        self.assertIn("Jul 27, 2026", flag)

    def test_freshness_flag_no_high_confidence(self):
        out = json.dumps({"pipeline": "A", "date": "2026-08-03", "candidates": [
            {"published": "2026-08-02", "confidence": "MEDIUM"}]})
        self.assertIn("HIGH-confidence", pipeline.freshness_flag(out))

    def test_freshness_flag_fresh_run_not_flagged(self):
        out = json.dumps({"pipeline": "A", "date": "2026-08-03", "freshness_note": "",
                          "candidates": [{"published": "2026-08-02", "confidence": "HIGH"}]})
        self.assertIsNone(pipeline.freshness_flag(out))
        self.assertIsNone(pipeline.freshness_flag(""))
        self.assertIsNone(pipeline.freshness_flag("not json at all"))

    def test_get_run_exposes_freshness_flag(self):
        p = self.runs / self.run_id / "stages.json"
        stages = json.loads(p.read_text(encoding="utf-8"))
        stages["scoutA"] = json.dumps({"pipeline": "A", "date": "2026-08-03",
                                       "freshness_note": "Search found only evergreen roundups.",
                                       "candidates": [{"published": "2026-07-01", "confidence": "MEDIUM"}]})
        p.write_text(json.dumps(stages), encoding="utf-8")
        run = self.svc.get_run(self.run_id)
        self.assertIn("evergreen roundups", run["scripts"]["scriptA"]["freshnessFlag"])
        self.assertIsNone(run["scripts"]["scriptB"]["freshnessFlag"])  # no scoutB stage saved

    # -------- rejection survives job bookkeeping --------
    def test_reject_sticks_on_past_generations(self):
        """Rejecting a script whose approval already has completed/failed jobs
        must stick — _sync_approval_status used to overwrite it."""
        a = self.approve_a()
        self.generate(a["id"])
        self.clock.advance(30)
        self.svc.poll_active_jobs()  # job completes → approval "completed"
        self.assertEqual(self.store.get_approval(a["id"])["status"], "completed")
        r = self.svc.reject(self.run_id, "scriptA", note="tone is off")
        self.assertEqual(r["approval"]["status"], "rejected")
        self.svc._sync_approval_status(a["id"])  # what polling/webhooks call
        self.assertEqual(self.store.get_approval(a["id"])["status"], "rejected")
        # re-approving is the only way out
        self.assertEqual(self.approve_a()["status"], "approved")

    # -------- deletion --------
    def test_delete_job(self):
        a = self.approve_a()
        job = self.generate(a["id"])["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        r = self.svc.delete_job(job["id"])
        self.assertNotIn("errors", r, r.get("errors"))
        self.assertIsNone(self.store.get_job(job["id"]))
        self.assertEqual(self.store.list_events(job["id"]), [])
        # with no attempts left the approval falls back to "approved"
        self.assertEqual(self.store.get_approval(a["id"])["status"], "approved")

    def test_delete_active_job_refused(self):
        a = self.approve_a()
        job = self.generate(a["id"])["job"]  # still queued
        r = self.svc.delete_job(job["id"])
        self.assertTrue(any("cancel it first" in e.lower() for e in r["errors"]))
        self.assertIsNotNone(self.store.get_job(job["id"]))

    def test_delete_job_keeps_attempt_numbers_unique(self):
        a = self.approve_a()
        j1 = self.generate(a["id"], idem="d1")["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        j2 = self.generate(a["id"], idem="d2")["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        self.svc.delete_job(j1["id"])
        j3 = self.generate(a["id"], idem="d3")["job"]
        self.assertGreater(j3["attemptNumber"], j2["attemptNumber"])

    def test_delete_run(self):
        a = self.approve_a()
        job = self.generate(a["id"])["job"]
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        r = self.svc.delete_run(self.run_id)
        self.assertNotIn("errors", r, r.get("errors"))
        self.assertEqual(r["deletedJobs"], 1)
        self.assertFalse((self.runs / self.run_id).exists())
        self.assertIsNone(self.store.get_approval(a["id"]))
        self.assertIsNone(self.store.get_job(job["id"]))
        self.assertNotIn(self.run_id, [x["id"] for x in self.svc.list_runs()])

    def test_delete_run_with_active_job_refused(self):
        a = self.approve_a()
        self.generate(a["id"])  # queued
        r = self.svc.delete_run(self.run_id)
        self.assertTrue(any("cancel it first" in e.lower() for e in r["errors"]))
        self.assertTrue((self.runs / self.run_id).exists())

    def test_delete_run_path_traversal_guard(self):
        for bad in ("..", "../..", "", "nope", f"..\\{self.run_id}"):
            r = self.svc.delete_run(bad)
            self.assertIn("errors", r, f"expected refusal for {bad!r}")
        self.assertTrue((self.runs / self.run_id).exists())

    # -------- podcast dialogue format --------
    def add_dialogue_run(self, mutate=None):
        run_id = "2026-07-16_pod"
        d = self.runs / run_id
        d.mkdir(exist_ok=True)
        script = {
            "pipeline": "B", "format": "podcast_dialogue", "voice": "dialogue",
            "title_working": "Podcast test", "runtime_seconds": 60,
            "beats": [
                {"beat": "OPEN", "speaker": "Rik", "vo": "Everyone is chasing acquisition.",
                 "on_screen_text": "", "visual_direction": "Two-shot"},
                {"beat": "EXCHANGE", "speaker": "Ravi", "vo": "Because retention asks a harder question.",
                 "on_screen_text": "", "visual_direction": "Single"},
                {"beat": "MOVE", "speaker": "Rik", "vo": "Here is the move.",
                 "on_screen_text": "", "visual_direction": "Single"},
                {"beat": "MOVE", "speaker": "Rik", "vo": "Know what each customer is worth.",
                 "on_screen_text": "", "visual_direction": "Single"},
                {"beat": "CLOSE", "speaker": "Ravi", "vo": "Growth you understand stays with you.",
                 "on_screen_text": "", "visual_direction": "Push-in"},
            ],
            "facts_used": [], "source_story_url": "",
        }
        if mutate:
            mutate(script)
        (d / "stages.json").write_text(json.dumps({
            "scriptB": json.dumps(script),
            "synopsisB": json.dumps({"pipeline": "B", "approved_facts": [], "do_not_use": []}),
        }), encoding="utf-8")
        return run_id

    def test_dialogue_approval_snapshots_both_hosts(self):
        run_id = self.add_dialogue_run()
        r = self.svc.approve(run_id, "scriptB")
        self.assertNotIn("errors", r, r.get("errors"))
        a = r["approval"]
        self.assertEqual(a["persona"], "Dialogue")
        self.assertEqual(a["personaConfigSnapshot"]["Rik"]["voiceId"], "test-voice-rik")
        self.assertEqual(a["personaConfigSnapshot"]["Ravi"]["voiceId"], "test-voice-ravi")
        self.assertIn("Rik:", a["approvedScriptSnapshot"]["narration"])
        self.assertIn("Ravi:", a["approvedScriptSnapshot"]["narration"])
        # consecutive Rik turns merge into one segment: Rik, Ravi, Rik(x2), Ravi
        segs = a["approvedScriptSnapshot"]["segments"]
        self.assertEqual([s["speaker"] for s in segs], ["Rik", "Ravi", "Rik", "Ravi"])
        self.assertIn("Here is the move.", segs[2]["text"])
        self.assertIn("Know what each customer is worth.", segs[2]["text"])

    def test_dialogue_unknown_speaker_blocks(self):
        run_id = self.add_dialogue_run(lambda s: s["beats"].append(
            {"beat": "CLOSE", "speaker": "Bob", "vo": "Hi.", "on_screen_text": "", "visual_direction": ""}))
        r = self.svc.approve(run_id, "scriptB")
        self.assertTrue(any("unknown speaker" in e.lower() for e in r["errors"]))

    def test_dialogue_requires_both_hosts_configured(self):
        run_id = self.add_dialogue_run()
        self.store.update_settings({"personas": {"Ravi": {"providerAvatarId": None, "referenceAssetId": None, "voiceId": None}}})
        a = self.svc.approve(run_id, "scriptB")["approval"]
        r = self.generate(a["id"])
        self.assertTrue(any("Ravi" in e for e in r["errors"]))

    def test_dialogue_mock_completes(self):
        run_id = self.add_dialogue_run()
        a = self.svc.approve(run_id, "scriptB")["approval"]
        job = self.generate(a["id"])["job"]
        self.assertEqual(job["persona"], "Dialogue")
        self.clock.advance(30)
        self.svc.poll_active_jobs()
        self.assertEqual(self.store.get_job(job["id"])["status"], "completed")

    def test_dialogue_tavus_rejected(self):
        from video_providers import TavusProvider
        with self.assertRaises(ProviderError) as ctx:
            TavusProvider().createVideo({"segments": [{"index": 0, "speaker": "Rik", "text": "hi",
                                                       "voiceId": "v", "avatarId": "a"}]})
        self.assertEqual(ctx.exception.code, "dialogue_unsupported")

    # -------- one-take podcast (HeyGen studio video, no stitching) --------
    def _capture_heygen_submit(self):
        """Patch the HTTP layer so createVideo captures the body instead of
        calling HeyGen. Returns (calls, restore_fn)."""
        import video_providers as vp
        calls = []

        def fake_http(method, url, headers, body=None, timeout=60):
            calls.append({"method": method, "url": url, "body": body})
            return {"data": {"video_id": "vid-studio-1", "status": "waiting"}}

        original = vp._http_json
        vp._http_json = fake_http
        return calls, lambda: setattr(vp, "_http_json", original)

    def test_dialogue_heygen_single_studio_video(self):
        os.environ["HEYGEN_API_KEY"] = "test-key"
        calls, restore = self._capture_heygen_submit()
        try:
            hey = self.providers["heygen"]
            r = hey.createVideo({
                "jobId": "job_x", "title": "Podcast test", "aspectRatio": "16:9",
                "resolution": "1080p", "captions": {"enabled": True},
                "expressiveness": "medium", "estimatedSeconds": 60,
                "segments": [
                    {"index": 0, "speaker": "Rik", "text": "Turn one.", "avatarId": "av-rik", "voiceId": "vo-rik"},
                    {"index": 1, "speaker": "Ravi", "text": "Turn two.", "avatarId": "av-ravi", "voiceId": "vo-ravi"},
                ]})
        finally:
            restore()
            del os.environ["HEYGEN_API_KEY"]
        self.assertEqual(len(calls), 1)  # ONE submission for the whole episode
        body = calls[0]["body"]
        self.assertEqual(body["type"], "studio")
        self.assertEqual(len(body["scenes"]), 2)
        s0 = body["scenes"][0]
        self.assertEqual(s0["type"], "avatar_video")
        self.assertEqual(s0["input"]["avatar_id"], "av-rik")
        self.assertEqual(s0["input"]["voice_id"], "vo-rik")
        self.assertEqual(s0["input"]["script"], "Turn one.")
        self.assertEqual(body["aspect_ratio"], "16:9")
        self.assertEqual(r["providerJobId"], "vid-studio-1")
        self.assertNotIn("segments", r)  # one job, no per-clip tracking

    def test_dialogue_heygen_requires_avatar_ids(self):
        os.environ["HEYGEN_API_KEY"] = "test-key"
        try:
            with self.assertRaises(ProviderError) as ctx:
                self.providers["heygen"].createVideo({
                    "jobId": "job_y", "captions": {"enabled": False},
                    "segments": [{"index": 0, "speaker": "Ravi", "text": "hi",
                                  "avatarId": None, "referenceAssetId": "asset_1", "voiceId": "v"}]})
            self.assertEqual(ctx.exception.code, "missing_avatar")
        finally:
            del os.environ["HEYGEN_API_KEY"]

    def test_dialogue_validation_blocks_photo_only_host_on_heygen(self):
        approval = {"status": "approved", "persona": "Dialogue", "scriptVersion": "h",
                    "approvedScriptSnapshot": {"narration": "Rik: hi\n\nRavi: hello", "runtimeSeconds": 10},
                    "settingsSnapshot": {"aspectRatio": "16:9"}}
        cfg = {"Rik": {"providerAvatarId": "av-rik", "voiceId": "v1"},
               "Ravi": {"referenceAssetId": "asset_photo", "voiceId": "v2"}}  # photo only
        problems = video_prep.validate_for_generation(
            approval, "h", cfg, self.providers["heygen"], {})
        self.assertTrue(any("Ravi" in p and "avatar ID" in p for p in problems), problems)
        self.assertFalse(any("Rik" in p and "avatar ID" in p for p in problems), problems)

    def test_single_voice_scripts_still_work(self):
        # Backward compatibility: pre-podcast runs keep the single-speaker flow.
        a = self.svc.approve(self.run_id, "scriptB")["approval"]
        self.assertEqual(a["persona"], "Rik")
        self.assertNotIn("Rik:", a["approvedScriptSnapshot"]["narration"])

    # switching provider AFTER approval must affect the next generation
    def test_generation_follows_current_provider_setting(self):
        a = self.approve_a()  # approved while settings.provider == "mock"
        self.store.update_settings({"provider": "stub"})
        r = self.generate(a["id"])
        self.assertNotIn("errors", r, r.get("errors"))
        self.assertEqual(r["job"]["provider"], "stub")
        self.assertEqual(self.store.get_approval(a["id"])["provider"], "stub")

    # mock placeholder IDs must never reach a real provider
    def test_mock_placeholders_blocked_on_real_provider(self):
        self.store.update_settings({"personas": {
            "Product": {"providerAvatarId": "mock-avatar", "voiceId": "mock-voice"}}})
        a = self.approve_a()  # snapshot now carries the placeholders
        r = self.generate(a["id"], provider="stub")
        self.assertTrue(any("placeholder" in e for e in r["errors"]), r.get("errors"))
        r2 = self.generate(a["id"])  # mock provider still fine
        self.assertNotIn("errors", r2, r2.get("errors"))

    # confirmation required
    def test_confirmation_required(self):
        a = self.approve_a()
        r = self.svc.generate(a["id"], {"idempotencyKey": "x"})
        self.assertTrue(any("confirmed" in e for e in r["errors"]))

    # concurrent generation blocked
    def test_no_concurrent_generation(self):
        a = self.approve_a()
        self.generate(a["id"], idem="k1")
        r = self.generate(a["id"], idem="k2")
        self.assertTrue(any("already in progress" in e for e in r["errors"]))


if __name__ == "__main__":
    unittest.main()
