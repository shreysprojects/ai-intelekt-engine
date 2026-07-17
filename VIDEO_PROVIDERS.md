# AI Video Provider Selection

Evaluated 2026-07-16 for the ai-InteleKt executive podcast-style video feature.
Goal: turn an approved script into a premium talking-head video (executive on a
sofa/armchair, warm interior, natural lip-sync), 1080p, 16:9 + 9:16, burned-in
captions, fully API-driven, asynchronous status, commercial use.

## Decision

- **Primary provider: HeyGen** (v3 API)
- **Fallback provider: Tavus** (v2 API)
- **Development/testing: built-in Mock provider** (no credits consumed)

## Providers assessed

### HeyGen — CHOSEN PRIMARY
- **Realism:** Avatar IV is the consistent leader in 2026 head-to-head
  comparisons for lip-sync, micro-expressions, and natural motion.
- **Reference-image workflow:** `POST /v3/videos` supports `type: "image"` —
  generates a talking video directly from an approved still image (base64, URL,
  or asset), which matches this project's persona reference-image flow exactly.
  `type: "avatar"` supports reusable custom avatars (photo avatar or digital
  twin trained from consent footage).
- **API:** `POST https://api.heygen.com/v3/videos` (auth: `x-api-key`), status
  via `GET /v3/videos/{video_id}` (`pending|processing|completed|failed`,
  presigned `video_url`, `captioned_video_url`, `thumbnail_url`, `duration`,
  `failure_code/message`). Native `Idempotency-Key` header (24h replay).
- **Formats:** `resolution: "720p"|"1080p"|"4k"`, `aspect_ratio: "16:9"|"9:16"|"1:1"|…`,
  `caption` object (SRT; captioned output URL), `voice_settings`,
  `expressiveness` (Avatar IV), `callback_url`/`callback_id`.
- **Webhooks:** `POST /v3/webhooks/endpoints` registration; deliveries signed
  with HMAC-SHA256 over the raw body (`Heygen-Signature`, `Heygen-Timestamp`,
  `Heygen-Event-Id`); events `avatar_video.success` / `avatar_video.fail`.
- **Pricing (official API pricing page, prepaid USD wallet, no subscription):**
  Photo avatar: Avatar III $0.0433/sec, Avatar IV $0.05/sec; digital twin
  $0.0667/sec; 720p and 1080p same rate. A 55-second executive short ≈ **$2.75**
  (Avatar IV photo) / **$3.67** (digital twin).
- **Consent/setup:** photo avatars require rights to the image; digital twins
  require consent footage recorded by the person. Account-side avatar/voice
  creation is needed before real generations (see README).
- **Weaknesses:** no documented cancel endpoint for a submitted video;
  presigned URLs expire (handled by re-fetching status).

### Tavus — CHOSEN FALLBACK
- **Model:** digital-twin "replicas" trained from a short consent video; strong
  natural motion; built for API-first personalized video.
- **API:** `POST https://tavusapi.com/v2/videos` (auth: `x-api-key`) with
  `replica_id` + `script` (+ `video_name`, `callback_url`); status via
  `GET /v2/videos/{video_id}` (`queued|generating|ready|error|deleted`,
  `download_url`, `stream_url`, `hosted_url`, thumbnails with `verbose=true`).
- **Weaknesses vs. HeyGen for this use case:** no one-shot generation from a
  single reference image (a replica must be trained first, with recorded
  consent script), no documented webhook signature (we re-poll the API on
  callback instead of trusting the payload), captions/aspect controls thinner.
- **Why fallback:** excellent quality and clean API, but the mandatory
  replica-training step means it cannot cold-start from the approved reference
  image the way HeyGen can.

### Synthesia — not selected
Very strong studio realism and enterprise compliance (SOC2/ISO, webhooks,
Create/Retrieve video API). Personal avatars are plan-gated with a
consent-video workflow, and the public API detail for a fully custom
photo-based flow is thinner than HeyGen's. Better suited to enterprise L&D
studio workflows than this repo's automated pipeline.

### D-ID — not selected
Mature photo→talking-head Talks API (V2–V4 avatar generations, streaming
support). Realism consensus in 2026 comparisons places it below HeyGen
Avatar IV for premium executive content; credit-based pricing. A viable third
option if both selections become unavailable.

### Captions — not selected
Creator/mobile-first product; API surface not aimed at this server-side
avatar workflow.

### Runway — not selected
General-purpose generative video (no purpose-built talking-head/lip-sync
avatar product) — wrong tool for a podcast-style executive monologue.

## Sources (official, consulted 2026-07-16)
- HeyGen create video: https://developers.heygen.com/reference/create-video.md
- HeyGen video status: https://developers.heygen.com/reference/get-video.md
- HeyGen webhooks: https://developers.heygen.com/docs/webhooks.md
- HeyGen API pricing: https://developers.heygen.com/docs/pricing.md
- HeyGen quick start: https://developers.heygen.com/docs/quick-start
- Tavus create video: https://docs.tavus.io/api-reference/video-request/create-video
- Tavus get video: https://docs.tavus.io/api-reference/video-request/get-video
- Synthesia quickstart: https://docs.synthesia.io/reference/synthesia-api-quickstart
- D-ID: https://www.d-id.com/api/ , https://docs.d-id.com/docs/quickstart , https://www.d-id.com/pricing/api/
- Landscape comparisons (secondary): ventureharbour.com, veed.io/learn/best-avatar-apis,
  synthesia.io/post/heygen-alternatives-competitors

## Assessment depth (transparency)
HeyGen and Tavus were verified endpoint-by-endpoint from official API
references (URLs above). Synthesia was verified at quickstart level. D-ID,
Captions, and Runway were assessed from official product/docs pages and 2026
comparison coverage — enough to rank them for this use case, not enough to
integrate without further verification.
