# Vera-Better — magicpin AI Challenge submission

**Team**: Team Vera-Better
**Stack**: FastAPI + Gemini 2.5 Flash (OpenAI-compatible endpoint)
**Endpoint**: `https://<your-public-url>/v1/*`

---

## Approach

A **single-prompt kind-aware composer** fuses all four contexts (Category +
Merchant + Trigger + optional Customer) into one WhatsApp message per call.
The `COMPOSER_SYSTEM` prompt enforces the 5-dimension rubric inline:
specificity, category fit, merchant fit, trigger relevance, engagement
compulsion. Trigger `kind` (`research_digest`, `recall_due`, `perf_spike`,
`competitor_opened`, `festival_upcoming`, `dormant_with_vera`, …) drives
framing variants in the system prompt without needing separate code paths.

A **separate reply composer** handles in-flight conversations and returns one
of three actions: `send` / `wait` / `end`. Two cases short-circuit *without*
an LLM call so they're deterministic and fast:

- **Auto-reply detection** — combination of canned-phrase list (English +
  Hindi) and Jaccard token-similarity against prior turns. On strike 1 the
  bot sends one polite probe; on strike 2 it exits gracefully (matches
  Pattern B from the brief). Strikes track per-merchant, not per-conversation,
  so they survive judge-generated `conversation_id` rotation.
- **Hard "stop" intent** → polite one-line exit ("Samajh gayi, all the best!").

A 5-class **intent classifier** (`go` / `stop` / `wait` / `question` /
`other`) routes engaged merchants into action mode without re-qualifying
(fixes Pattern D — *"Mujhe magicpin judrna hai"* never goes back to a
qualification question). A clear `go` intent overrides the auto-reply path
and resets strike counters — engaged humans are by definition not auto-replying.

**Anti-repetition** is enforced post-LLM: if the proposed body normalizes
to a body already sent in the same conversation, the bot switches to
`wait` instead of repeating itself.

**Tick orchestration**: at most one action per merchant per tick, ranked by
`urgency` × not-yet-fired × suppression-key-fresh × not-expired. Empty
`actions: []` when nothing's worth saying — restraint is rewarded per the brief.

## Tradeoffs

- **In-memory state**, per spec. Simple and fast; lost on restart, fine for
  a 60-min test window. A keep-alive ping on `/v1/healthz` from any uptime
  monitor handles the Render free-tier cold-start case.
- **JSON-mode opt-out for Gemini**: Gemini's OpenAI-compatible endpoint is
  flaky with `response_format`, so JSON mode is disabled for that provider
  and a tolerant regex-based JSON extractor handles markdown-fenced output.
- **Deterministic fallbacks everywhere**: every LLM call has a
  context-aware non-LLM fallback (`_fallback_compose`, `_fallback_reply`).
  Worst case (LLM fully down), the bot still produces sane intent-aware
  messages and never crashes a tick.
- **One action per merchant per tick** rather than batching: simpler
  conversation tracking, no double-fire on the same merchant. The 18-action
  cap leaves headroom under the spec's 20-action ceiling.
- **No persistent storage**: prompt versioning is hard-coded as
  `vera_<kind>_v1`. In a production setting we'd want a versioned prompt
  registry with per-message rationale logging; for a 60-min test it's
  unnecessary overhead.

## What additional context would have helped most

1. **`vertical_messaging_examples` on CategoryContext** — 3-5 gold-standard
   sample messages per kind would be the single biggest lift on
   category-fit scoring. The brief shows what good looks like for dentists
   (Appendix A); a structured field would let the composer few-shot from it.
2. **`merchant.communication_history_summary`** — a compressed summary of
   the last-90-day engagement themes per merchant (what's been pitched,
   what worked, what was rejected). Currently `conversation_history` is
   raw turns; a derived summary would prevent topic-repetition across
   weeks.
3. **`peer_offers` on CategoryContext** — what comparable merchants in the
   same locality are running. Unlocks compulsion lever #3 (social proof),
   which the brief explicitly calls out as underused in production today.
4. **`customer.recent_search_terms`** — for customer-facing sends, knowing
   what the customer searched for on magicpin in the last 30 days would
   make recall messages dramatically more specific.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
export LLM_PROVIDER=gemini LLM_API_KEY=AIza... LLM_MODEL=gemini-2.5-flash
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Smoke Test:
```
python smoke_test.py
```
## Files
- bot.py — the FastAPI app (5 endpoints + composer + reply handler)
- conversation_handlers.py — standalone respond() for replay scoring (§7.4)
- smoke_test.py — local end-to-end test against fixtures
- make_submission.py — generates submission.jsonl for the 30 test pairs
- requirements.txt, Dockerfile, render.yaml, fly.toml — deploymen


---

## How to use this

1. Save the markdown block above as `README.md` in your project folder.
2. **Edit two things** before submitting:
   - **Team name** (line 3): change `Team Vera-Better` to whatever you registered with magicpin
   - **Endpoint URL** (line 5): replace `<your-public-url>` with your actual deployed URL (e.g., `vera-better-bot.onrender.com` or `vera-better-bot-yourname.fly.dev`)
3. Commit it:
   ```powershell
   git add README.md
   git commit -m "Add submission README"
   git push
   ```
