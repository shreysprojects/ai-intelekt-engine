# ai-InteleKt Content Engine (Python)

Daily 7-agent content pipeline from the CMO Content Engine build doc, extended
with an approval → AI-video workflow:

```
Generate content → Review script → Approve for Video
→ Submit to the AI video provider → Track status → Preview / download
```

Nothing is ever auto-published: every script passes the validation gate, a
human approves it, and a human confirms every (potentially paid) generation.

## Requirements
- Python 3.10+ (`py --version`)
- `py -m pip install anthropic`
- An Anthropic API key (console.anthropic.com) for content generation
- Optional: a HeyGen or Tavus API key for real video generation (see below)

## Run the dashboard
```
py app.py
```
Open http://localhost:3000. Paste the Anthropic key, click **Run today's
pipeline** (~8–15 min on Opus, ~$1.50). The run then appears in the
**Video Production** card.

## Podcast format (Pipeline B)
The thought-leadership script is now a **two-host podcast dialogue**: Rik and
Ravi in conversation (Rik's commercial read vs. Ravi's principled reframe),
written turn-by-turn with per-turn speakers, on-screen text, and shot
directions. The product script (Pipeline A) stays in the neutral product voice
— the Foundation Block forbids the executives from pitching product.

For video, each host's consecutive turns become one clip generated with that
host's avatar and voice (HeyGen v3 renders one avatar per video); the clips
are tracked as ordered segments of a single job and downloaded for the podcast
edit (two-shot stitching happens in the edit). Older single-voice runs keep
working unchanged. A hand-written `runs/2026-07-16_demo-podcast` run
demonstrates the format — delete it once a real run exists.

## Video workflow
1. **Video Production** shows both scripts from the selected run: narration,
   beats, on-screen text, visual directions, sources + verification status.
   Scripts that fail the validation gate ([VERIFY] markers, DO NOT USE items,
   unverified required sources, statistics without an approved source) cannot
   be approved — the exact problems are listed on the card.
2. **Approve for Video** stores a permanent snapshot (script version hash,
   narration, persona, sources, settings). Editing the script afterwards
   invalidates the approval automatically.
3. The review panel shows the final narration, speaker/persona, reference
   image, voice, runtime, format, captions, and estimated cost. **Generate
   Video** asks for confirmation, then submits to the provider (idempotent —
   double-clicks cannot create duplicate paid jobs).
4. The **Video queue** tracks every attempt (queued → processing → completed /
   failed / cancelled) with per-job history, preview, download, retry,
   regenerate, and cancel. Old attempts are always retained.
5. **Video settings** holds the provider choice, per-persona reference images
   / avatar IDs / voice IDs, defaults, and spending limits.

### Providers
See `VIDEO_PROVIDERS.md` for the full comparison. Summary:
- **Mock Video Provider** (default) — free, simulates the entire lifecycle,
  clearly labelled; produces a placeholder, never a real video.
- **HeyGen** (primary) — generates from a persona reference image (Avatar IV)
  or a trained avatar; 1080p, 16:9/9:16/1:1, burned-in captions, signed
  webhooks, ~$0.05/sec (≈$2.75 per 55s video).
- **Tavus** (fallback) — requires a trained replica (consent footage) per
  persona; 16:9.

### Real-provider setup (account-side steps we cannot automate)
1. Create the provider account and API key; put it in `.env`
   (copy `.env.example` → `.env`).
2. Upload each speaker's reference image in Video settings **with their
   permission** — for real people, HeyGen/Tavus additionally run their own
   consent flow (HeyGen digital twins and Tavus replicas require consent
   footage recorded by the person).
3. Enter the provider voice IDs (and avatar/replica IDs if using trained
   avatars) per persona in Video settings.
4. Optional webhooks: expose this server over HTTPS, set
   `VIDEO_WEBHOOK_PUBLIC_URL`, register the endpoint with the provider, store
   the returned secret in `VIDEO_PROVIDER_WEBHOOK_SECRET`. Without webhooks
   the server polls every 15s, which is fine for local use.

### Cost protection
- Estimated cost is shown before every generation and each submission requires
  explicit confirmation.
- `VIDEO_MONTHLY_HARD_LIMIT` (or Settings) blocks new **real** generations
  once the month's spend would exceed it; the mock provider is exempt.
- Estimates are computed from the provider's published rates and are labelled
  as estimates.

## Run headless (no browser — for schedulers/cron)
```
$env:ANTHROPIC_API_KEY='sk-ant-...'; py pipeline.py
```

## Tests
```
py -m unittest discover -s tests -v
py -m compileall app.py pipeline.py video_store.py video_prep.py video_providers.py video_service.py
```

## Files
- `pipeline.py` — content engine: Foundation Block (edit once, all seven agents
  stay in sync), agent prompts, web search/URL verification, run saving.
- `app.py` — local web server, video API endpoints, webhook receiver, poller.
- `video_prep.py` — script → clean narration; approval + generation validation.
- `video_providers.py` — provider adapters (mock / HeyGen / Tavus).
- `video_service.py` — approvals, generation jobs, idempotency, cost limits.
- `video_store.py` — JSON persistence (`data/`) + reference-image storage.
- `public/` — dashboard (display-only; index.html + video.js).
- `runs/` — every content run's handoff pack + raw stage outputs (audit log).
- `VIDEO_PROVIDERS.md` — provider research, selection rationale, sources.

## Security notes
- Provider API keys live in environment variables / `.env` (git-ignored) and
  are never sent to the browser, logged, or stored in `data/`.
- Webhook deliveries are HMAC-verified (HeyGen) or treated as untrusted
  re-poll triggers (Tavus); duplicate deliveries are ignored.
- Uploaded images are validated by content (magic bytes + dimensions), capped
  at 8MB, stored only in `data/assets/` (git-ignored).
