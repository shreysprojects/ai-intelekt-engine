"""ai-InteleKt Content Engine — pipeline logic (all seven agents).

Every agent is one Anthropic API call sharing the FOUNDATION block below.
Edit FOUNDATION once and all seven agents stay in sync (per the build doc).

Can also be run headless:  py pipeline.py  (reads ANTHROPIC_API_KEY env var)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

RUNS_DIR = Path(__file__).resolve().parent / "runs"

# ======================= SHARED BRAND CONTEXT =======================
FOUNDATION = """=== AI-INTELEKT FOUNDATION BLOCK (shared context for all agents) ===

WHO WE ARE
ai-InteleKt gives small and mid-market retail & wholesale businesses
enterprise-grade customer intelligence - without enterprise cost or complexity.
We are data and business-intelligence people first. We turn messy, fragmented
customer data into retention, loyalty, and lifetime-value growth.

THE PRODUCT SUITE (use the right product for the right problem)
- CLV - Customer Lifetime Value suite (FLAGSHIP). Scores every customer by
  future value, churn risk, and purchase intent. 12 intelligence modules incl.
  RFM, CLV:CAC, predictive churn scoring, cohort CLV, offer personalization,
  campaign ROI, loyalty intelligence. Built for Retail & Telecom. No user-based
  or usage-based pricing. Plug-and-play POS/CRM connectors.
- LMP - Loyalty Management Platform. Points, tiers, promotions, coupons,
  zero/first-party data, surveys. Turns repeat buyers into advocates. Pairs with
  CLV for retention.
- CEP - Customer Engagement Platform. Omnichannel: email, SMS, postal,
  e-receipts, chat, drip, hyper-personalization. (Note: it is C-E-P, Customer
  Engagement Platform.)
- Customer 360 - unified customer view / CRM alternative to Zoho & HubSpot.
- CAP - Customer Analytics Platform. The analytics foundation: sales, engagement,
  email, segmentation, hyper-personalization, predictive analytics.
- AI-Plush (APP) - the AI layer: predictive analytics, hyper-personalization,
  KPI/process automation, campaign management, resource optimization.

IDEAL CUSTOMER (ICP)
- SMB / MSME retail & wholesale, roughly $10M-$750M revenue.
- Regions: USA, Canada, EMEA, India, Malaysia, Singapore (India = priority in APAC).
- Buyers: CMO/VP, Customer Success teams, Operations managers.

THE THREE CORE PAINS WE SPEAK TO
1. Customer-retention struggle. 2. Fragmented systems / scattered customer data.
3. Data exists but no actionable insight.

THE MARKET STORY WE TELL (the wedge)
- SMB/MSME gap: BI is priced high and adopted low. IN OUR VIEW the few SMBs who
  DO adopt BI + loyalty grow faster with higher per-customer value. THIS IS OUR
  POINT OF VIEW - no external source on file. Agents must frame it as our belief,
  NEVER as a statistic.
- Enterprise-incumbent blind spot: big platforms over-build and over-charge for
  scale mid-market retailers don't need. We give the same edge - simple,
  affordable, fast to value.

COMPETITORS (name only when validated, frame as "the case for us", never a
fabricated knock): Capillary Technologies, CleverTap, MoEngage, WebEngage,
Netcore, Salesforce Marketing Cloud.

THE TWO EXECUTIVE VOICES (never mix them)
- Ravi Srinivasan - CEO & Founder. Vision, philosophy, where retail is heading,
  democratizing BI, the long game. Calm, convicted, measured; fewer, heavier
  sentences; ends on a principle, not a CTA. Never tactical sales talk, no hype,
  no exclamation-mark energy.
- Rik Chatterjee - Chief Growth Officer. Revenue, GTM, market opportunity,
  competitive plays, the numbers. Sharp, commercial, momentum-driven, crisp
  sentences, concrete nouns, "here's the move" energy. Never deep product
  philosophy. No hard product pitching in thought-leadership pieces.

NON-NEGOTIABLES (VALIDATION GATE)
- No invented statistics, fake customer names, or fabricated competitor claims.
- Every factual claim must trace to a cited, dated, real URL.
- Never upgrade MEDIUM confidence to fact. If nothing verifiable is found, say so
  plainly instead of fabricating.
- LinkedIn + Instagram destinations; short-form, scroll-stopping, enterprise-grade.
=== END FOUNDATION BLOCK ==="""

# ======================= SERVER TOOLS =======================
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}
WEB_FETCH = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4}

# $ per 1M tokens (input, output)
RATES = {"claude-opus-4-8": (5.0, 25.0), "claude-sonnet-5": (3.0, 15.0)}

# ======================= STAGE PROMPTS =======================
# Templates use __TOKENS__ filled in by run_pipeline (no f-string brace issues).

PROMPT_SCOUT_A = """Today's date: __TODAY__.

You are SCOUT A. Surface today's most relevant RETAIL news that connects to one of our product lines.

Search North American and APAC (India-first, then ASEAN) retail sources from the last 48 hours for: customer retention & churn, loyalty program launches/failures, customer-data unification, omnichannel engagement, personalization, CRM modernization, SMB/MSME tech adoption, enterprise martech missteps.

For EVERY candidate: source name, title, URL, publication date; one line on why it matters to a mid-market retailer; product fit (CLV | LMP | CEP | Customer360 | CAP); lens (SMB-ADOPTION-GAP or ENTERPRISE-BLIND-SPOT); confidence HIGH only if primary/reputable and dated. Never invent stories, statistics, or URLs. Return 3-5 ranked candidates.

Output JSON only:
{"pipeline":"A","date":"YYYY-MM-DD","candidates":[{"title":"","source":"","url":"","published":"YYYY-MM-DD","why_it_matters":"","product_fit":"","lens":"","confidence":"HIGH|MEDIUM","key_facts":["fact + figure (with source)"]}]}"""

PROMPT_SYNOPSIS_A = """You are SYNOPSIS/ROUTER A. Input: the Scout A candidate list below. Pick the ONE strongest story for a product-led post today and route it.

IMPORTANT VERIFICATION STEP: before finalizing, use the web_fetch tool to open the chosen story's URL and confirm the page loads and supports the key facts. If it fails or contradicts them, pick a different candidate or mark the affected facts DO NOT USE. Record a "verified" field per approved fact.

Then: one line on why chosen; 3-4 sentence plain synopsis; lock product mapping; one-paragraph bridge (how the product answers the problem, through the given lens); list approved_facts (each with source + url + verified true/false) - the script agent may use ONLY these; flag anything unverifiable as DO NOT USE.

Output JSON only:
{"pipeline":"A","chosen_story":{"title":"","url":"","published":""},"synopsis":"","product":"","bridge":"","lens":"","approved_facts":[{"fact":"","source":"","url":"","verified":true}],"do_not_use":[""]}

SCOUT A OUTPUT:
__INPUT__"""

PROMPT_SCRIPT_A = """You are SCRIPT A. Turn the routed product story below into a LinkedIn + Instagram short-form VIDEO script (35-60s; ~90-150 spoken words).

Structure: HOOK (stop the scroll in 3s) > TENSION (retailer's real problem) > SHIFT (how the mapped product changes it) > PROOF (only an approved fact) > CTA (soft - comment/DM, not hard-sell).
Rules: use ONLY approved_facts; name the product naturally; lead with the customer's problem; enterprise-grade tone, no buzzword soup or emoji spam; provide VO, on-screen text, and one-line visual direction per beat.

Output JSON only:
{"pipeline":"A","voice":"product","title_working":"","runtime_seconds":0,"beats":[{"beat":"HOOK|TENSION|SHIFT|PROOF|CTA","vo":"","on_screen_text":"","visual_direction":""}],"facts_used":[{"fact":"","source":""}],"source_story_url":""}

ROUTED STORY:
__INPUT__"""

PROMPT_SCOUT_B = """Today's date: __TODAY__.

You are SCOUT B. Surface today's HOT retail / commerce / consumer market topics an industry authority should weigh in on - even if unrelated to our products.

Search NA and APAC (India-first, then ASEAN) from the last 48 hours for: major retail moves, consumer-behavior shifts, AI-in-retail, regulation, big-retailer wins/failures, agentic commerce, economic signals affecting retail.

For EVERY candidate: source, title, URL, publication date; one line on why a CEO or CGO should weigh in; voice fit RAVI (vision/philosophy) or RIK (revenue/GTM/competitive); confidence HIGH only if primary/reputable and dated. Never invent stories, figures, or URLs. Return 3-5 ranked candidates.

Output JSON only:
{"pipeline":"B","date":"YYYY-MM-DD","candidates":[{"title":"","source":"","url":"","published":"","why_a_leader_weighs_in":"","voice_fit":"RAVI|RIK","confidence":"HIGH|MEDIUM","key_facts":["fact + figure (with source)"]}]}"""

PROMPT_SYNOPSIS_B = """You are SYNOPSIS/ROUTER B. Input: the Scout B candidates below. Pick the ONE strongest topic for today's executive PODCAST segment - a short filmed conversation between Ravi Srinivasan (CEO) and Rik Chatterjee (CGO).

IMPORTANT VERIFICATION STEP: before finalizing, use the web_fetch tool to open the chosen topic's URL and confirm it loads and supports the key facts. If it fails or contradicts them, pick a different candidate or mark affected facts DO NOT USE. Record "verified" per approved fact.

Then: one line on why chosen; 3-4 sentence neutral synopsis; write the PODCAST PREMISE - the specific tension the two hosts work through: rik_angle (the sharp commercial read: the market move, the numbers, who wins) and ravi_angle (the principled reframe: what it means for where retail is heading). Both angles must be defensible and distinct, may reference our worldview WITHOUT pitching a product (thought leadership is not a product ad); approved facts with sources + urls + verified; flag unverifiable items DO NOT USE.

Output JSON only:
{"pipeline":"B","chosen_topic":{"title":"","url":"","published":""},"synopsis":"","voice":"dialogue","premise":"","rik_angle":"","ravi_angle":"","approved_facts":[{"fact":"","source":"","url":"","verified":true}],"do_not_use":[""]}

SCOUT B OUTPUT:
__INPUT__"""

PROMPT_SCRIPT_B = """You are SCRIPT B. Write a short two-host PODCAST dialogue between Rik Chatterjee (CGO) and Ravi Srinivasan (CEO) reacting to the routed topic below (90-150 seconds spoken; ~230-380 words total). This is a filmed executive podcast conversation, not a scripted ad and not an interview - no off-screen questions.

Obey BOTH voice guides from the Foundation Block exactly - a reader should know who is speaking without seeing the name. Rik: sharp, commercial, momentum-driven, concrete numbers, "here's the move" energy. Ravi: calm, principled, fewer and heavier sentences, reframes tactics into direction. They build on and gently challenge each other like real co-hosts; addressing each other by first name occasionally is good.

Structure: OPEN (Rik hooks with the sharpest fact or tension) > EXCHANGE (3-6 alternating turns genuinely working the topic: Rik's commercial read, Ravi's reframe, real back-and-forth) > MOVE (Rik: what a mid-market retailer should actually do) > CLOSE (Ravi lands a principle; no CTA, no product pitch, no "let's talk" sales energy).

Rules: each turn is 1-3 sentences; turns alternate speakers; use ONLY approved_facts for numbers/claims and attribute them naturally in speech (e.g. "a report out this week says..." - never read URLs or full source names aloud); authority-building, never selling; per turn provide on-screen text and a one-line visual direction (two-shot vs single on the speaker).

Output JSON only:
{"pipeline":"B","format":"podcast_dialogue","voice":"dialogue","title_working":"","runtime_seconds":0,"beats":[{"beat":"OPEN|EXCHANGE|MOVE|CLOSE","speaker":"Rik|Ravi","vo":"","on_screen_text":"","visual_direction":""}],"facts_used":[{"fact":"","source":""}],"source_story_url":""}

ROUTED TOPIC:
__INPUT__"""

PROMPT_PACKAGING = """You are the PACKAGING AGENT. Input: the two finished script objects below. Produce a complete handoff pack the team can execute without questions.

For EACH script produce:
- LinkedIn caption (max 1300 chars, strong first line, line breaks, 3-5 hashtags, soft CTA)
- Instagram caption (punchier, max 2200 chars, 8-12 hashtags niche+broad, Reels-friendly)
- VIDEO BRIEF: runtime, 9:16, pacing, mood, music suggestion, beat-by-beat b-roll/motion cues, on-screen text list (verbatim), caption burn-in note
- Posting windows for NA and India/APAC (note any news-staleness deadline)
- Title/thumbnail line
- SOURCES table: every fact used, its source, URL, and verification status carried over from the scripts

Rules: introduce NO new facts; mark inferred production choices [INFERRED]; POV claims stay framed as opinion; enterprise standard.

Note: SCRIPT OBJECT 2 is a two-host podcast dialogue between Ravi Srinivasan (CEO) and Rik Chatterjee (CGO). Attribute every quote to the correct host, credit both hosts in captions, and write its video brief for an executive podcast set (warm premium interior; two-shot establishing, then alternating singles on whichever host is speaking).

Output clean, well-structured MARKDOWN (not JSON), starting with the heading "# ai-InteleKt - Content Handoff Pack - __DATE__".

SCRIPT OBJECT 1 (product):
__SCRIPT_A__

SCRIPT OBJECT 2 (thought leadership):
__SCRIPT_B__"""

# key, display name, tools, effort
STAGES = [
    ("scoutA", "1 · Scout A — find product-relevant retail news", [WEB_SEARCH], "medium"),
    ("synopsisA", "2 · Router A — pick story, verify URL, approve facts", [WEB_FETCH], "medium"),
    ("scriptA", "3 · Script A — product video script", None, "high"),
    ("scoutB", "4 · Scout B — find hot market topics", [WEB_SEARCH], "medium"),
    ("synopsisB", "5 · Router B — pick topic, verify URL, lock voice", [WEB_FETCH], "medium"),
    ("scriptB", "6 · Script B — Ravi/Rik voiced script", None, "high"),
    ("packaging", "7 · Packaging — captions, video briefs, sources", None, "medium"),
]


def extract_json(text: str) -> str:
    """Pull a JSON object out of a model reply (strips code fences, finds braces)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"```\s*$", "", t)
    try:
        json.loads(t)
        return t
    except (json.JSONDecodeError, ValueError):
        pass
    a, b = t.find("{"), t.rfind("}")
    if 0 <= a < b:
        cut = t[a : b + 1]
        try:
            json.loads(cut)
            return cut
        except (json.JSONDecodeError, ValueError):
            pass
    return t  # pass raw text forward; downstream model can still read it


def friendly_error(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "API key rejected — check it at console.anthropic.com → API keys."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the API — wait a minute and run again."
    if isinstance(e, anthropic.APIStatusError):
        msg = getattr(e, "message", str(e))
        if "credit" in str(msg).lower():
            return f"{msg} — the account may be out of credits."
        return f"API error {e.status_code}: {msg}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach api.anthropic.com — check the internet connection."
    return str(e)


def call_stage(client: anthropic.Anthropic, model: str, prompt: str, tools, effort: str | None):
    """One agent call. Streams (avoids timeouts), resumes pause_turn, totals usage."""
    params: dict = {
        "model": model,
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "system": [{"type": "text", "text": FOUNDATION, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        params["tools"] = tools
    if effort:
        params["output_config"] = {"effort": effort}

    usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    def add(u):
        if not u:
            return
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0

    with client.messages.stream(**params) as s:
        msg = s.get_final_message()
    add(msg.usage)

    # Server-side tools (web search) can pause a long turn; resume until done.
    guard = 0
    messages = params["messages"]
    while msg.stop_reason == "pause_turn" and guard < 8:
        guard += 1
        messages = messages + [{"role": "assistant", "content": msg.content}]
        with client.messages.stream(**{**params, "messages": messages}) as s:
            msg = s.get_final_message()
        add(msg.usage)

    text = "\n".join(b.text for b in msg.content if b.type == "text")
    searches = sum(1 for b in msg.content if b.type == "server_tool_use")
    tool_errors = []
    for b in msg.content:
        if b.type in ("web_search_tool_result", "web_fetch_tool_result"):
            code = getattr(b.content, "error_code", None)
            if code:
                tool_errors.append(code)
    return text, usage, searches, tool_errors


def run_pipeline(api_key: str, model: str = "claude-opus-4-8", progress=None) -> dict:
    """Run all seven agents in order. Returns pack + totals; saves an audit copy.

    progress(event, key, data) is called with events:
      "stage_start" / "stage_done" / "stage_error" — key = stage key
    """
    def emit(event, key=None, **data):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {event} {key or ''} {data if data else ''}".strip())
        if progress:
            progress(event, key, data)

    client = anthropic.Anthropic(api_key=api_key.strip(), max_retries=3)
    today = datetime.now().strftime("%Y-%m-%d")
    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    outputs: dict[str, str] = {}

    def stage(key, prompt, tools, effort):
        emit("stage_start", key)
        t0 = time.time()
        try:
            text, usage, searches, tool_errors = call_stage(client, model, prompt, tools, effort)
        except Exception as e:  # noqa: BLE001 — mapped to a friendly message for the UI
            emit("stage_error", key, error=friendly_error(e))
            raise RuntimeError(friendly_error(e)) from e
        for k in totals:
            totals[k] += usage[k]
        outputs[key] = text
        emit("stage_done", key, seconds=round(time.time() - t0), searches=searches,
             tool_errors=tool_errors, output=text)
        return text

    s1 = stage("scoutA", PROMPT_SCOUT_A.replace("__TODAY__", today), [WEB_SEARCH], "medium")
    s2 = stage("synopsisA", PROMPT_SYNOPSIS_A.replace("__INPUT__", extract_json(s1)), [WEB_FETCH], "medium")
    s3 = stage("scriptA", PROMPT_SCRIPT_A.replace("__INPUT__", extract_json(s2)), None, "high")
    s4 = stage("scoutB", PROMPT_SCOUT_B.replace("__TODAY__", today), [WEB_SEARCH], "medium")
    s5 = stage("synopsisB", PROMPT_SYNOPSIS_B.replace("__INPUT__", extract_json(s4)), [WEB_FETCH], "medium")
    s6 = stage("scriptB", PROMPT_SCRIPT_B.replace("__INPUT__", extract_json(s5)), None, "high")
    pack = stage(
        "packaging",
        PROMPT_PACKAGING.replace("__SCRIPT_A__", extract_json(s3))
        .replace("__SCRIPT_B__", extract_json(s6))
        .replace("__DATE__", today),
        None,
        "medium",
    )

    rate_in, rate_out = RATES.get(model, RATES["claude-opus-4-8"])
    cost = (
        totals["input"] * rate_in
        + totals["cache_write"] * rate_in * 1.25
        + totals["cache_read"] * rate_in * 0.1
        + totals["output"] * rate_out
    ) / 1e6

    run_dir = RUNS_DIR / datetime.now().strftime("%Y-%m-%d_%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "handoff-pack.md").write_text(pack, encoding="utf-8")
    (run_dir / "stages.json").write_text(json.dumps(outputs, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"pack": pack, "usage": totals, "cost": round(cost, 2), "saved": str(run_dir)}


if __name__ == "__main__":
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Set ANTHROPIC_API_KEY first, e.g.:  $env:ANTHROPIC_API_KEY='sk-ant-...'; py pipeline.py")
        sys.exit(1)
    model_arg = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-4-8"
    result = run_pipeline(key, model_arg)
    print(f"\nDone. Cost ~${result['cost']}. Pack saved to: {result['saved']}")
