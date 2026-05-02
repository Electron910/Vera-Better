"""
Vera-Better — magicpin AI Challenge submission.

Implements the 5-endpoint contract from testing-brief.md with:
  - Stateful context store (idempotent on (scope, id, version))
  - LLM composer with category/merchant/trigger/customer fusion
  - Auto-reply detection + intent transition handling
  - Anti-repetition + suppression key dedup
  - Multi-provider LLM (OpenAI-compatible + Anthropic)
"""
from __future__ import annotations

import os
import re
import json
import time
import hashlib
import asyncio
import logging
from collections import defaultdict, Counter
from datetime import datetime, timezone
from typing import Any, Optional, Literal

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

LLM_PROVIDER  = os.getenv("LLM_PROVIDER",  "openai")          
LLM_API_KEY   = os.getenv("LLM_API_KEY",   "")
LLM_MODEL     = os.getenv("LLM_MODEL",     "gpt-4o-mini")
LLM_BASE_URL  = os.getenv("LLM_BASE_URL",  "")               
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "22"))

TEAM_NAME     = os.getenv("TEAM_NAME",    "Team Vera-Better")
TEAM_MEMBERS  = [s.strip() for s in os.getenv("TEAM_MEMBERS", "Solo").split(",")]
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "team@example.com")
APPROACH      = "Single-prompt composer w/ kind-aware framing, auto-reply heuristics, intent-transition routing, anti-repetition."
VERSION       = "1.0.0"

PROVIDER_DEFAULTS = {
    "openai":     ("https://api.openai.com/v1",            "gpt-4o-mini"),
    "deepseek":   ("https://api.deepseek.com/v1",          "deepseek-chat"),
    "groq":       ("https://api.groq.com/openai/v1",       "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1",         "anthropic/claude-3.5-sonnet"),
    "ollama":     ("http://localhost:11434/v1",            "llama3.1"),
    "anthropic":  ("https://api.anthropic.com/v1",         "claude-3-5-sonnet-20241022"),
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
}
if not LLM_BASE_URL:
    LLM_BASE_URL = PROVIDER_DEFAULTS.get(LLM_PROVIDER, PROVIDER_DEFAULTS["openai"])[0]
if not LLM_MODEL or LLM_MODEL == "gpt-4o-mini":
    if LLM_PROVIDER in PROVIDER_DEFAULTS:
        LLM_MODEL = PROVIDER_DEFAULTS[LLM_PROVIDER][1]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("vera")

app = FastAPI(title="Vera-Bot", version=VERSION)

START_TS = time.time()

contexts: dict[tuple[str, str], dict] = {}
conversations: dict[str, dict] = {}
fired: dict[tuple[str, str], str] = {}
trigger_fired: dict[tuple[str, str], str] = {}
merchant_convs: dict[str, list[str]] = defaultdict(list)
merchant_auto_strikes: dict[str, int] = defaultdict(int)

def _new_conv(cid: str, merchant_id: str, customer_id: Optional[str],
              trigger_id: str, send_as: str) -> dict:
    c = {
        "conversation_id": cid,
        "merchant_id":   merchant_id,
        "customer_id":   customer_id,
        "trigger_id":    trigger_id,
        "send_as":       send_as,
        "turns":         [],   
        "status":        "active",
        "auto_reply_strikes": 0,
        "nudges_after_silence": 0,
        "last_bot_body": "",
        "started_at":    datetime.now(timezone.utc).isoformat(),
    }
    conversations[cid] = c
    merchant_convs[merchant_id].append(cid)
    return c


def _ctx(scope: str, ctx_id: Optional[str]) -> Optional[dict]:
    if not ctx_id:
        return None
    rec = contexts.get((scope, ctx_id))
    return rec["payload"] if rec else None


class LLMError(Exception): ...

async def llm_chat(system: str, user: str, *, json_mode: bool = True,
                   temperature: float = 0.0, max_tokens: int = 700) -> str:
    """Single async LLM call returning text (JSON if json_mode)."""
    if not LLM_API_KEY and LLM_PROVIDER != "ollama":
        raise LLMError("LLM_API_KEY not set")

    if LLM_PROVIDER == "anthropic":
        url = f"{LLM_BASE_URL}/messages"
        headers = {
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": LLM_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as cli:
            r = await cli.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            log.error("LLM %s %s — body=%s", LLM_PROVIDER, r.status_code, r.text[:800])
            r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    if json_mode and LLM_PROVIDER not in ("gemini", "ollama"):
        payload["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as cli:
        r = await cli.post(url, headers=headers, json=payload)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _parse_json(s: str) -> dict:
    s = s.strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        s = m.group(0)
    return json.loads(s)


COMPOSER_SYSTEM = """You are VERA, magicpin's merchant-AI assistant on WhatsApp in India.
You compose ONE concise outbound message that a real Indian small-business merchant (or their customer) would actually reply to.

ABSOLUTE RULES (violations = hard fail):
1. NEVER invent facts. Use only numbers, dates, sources, names, offers, slots that appear in the inputs.
2. If a fact isn't in inputs, omit it. Don't fabricate research papers, competitor names, peer counts, or stats.
3. Match the language preference. "hi-en mix" → natural Hinglish (Devanagari rare; Roman Hindi). "en" → English. Do not force Hindi where merchant uses English.
4. Match category voice. Dentists/doctors/lawyers = clinical-peer, source-cited, no "guaranteed"/"cure". Salons/restaurants/gyms = warm-peer. NEVER promotional ("AMAZING DEAL!!").
5. Single primary CTA. Action triggers: binary YES / STOP. Pure-info triggers: open-ended OR no CTA. NEVER multi-choice unless it's a slot-booking message.
6. Anchor on a VERIFIABLE concrete fact (number, date, headline, source citation, peer stat). Generic = penalised.
7. Service+price beats discount. Use offer_catalog titles like "Dental Cleaning @ ₹299", not "20% off".
8. CTA lands in the last sentence. No long preambles. No "I hope you're doing well".
9. If conversation_history shows prior Vera turns, DO NOT re-introduce yourself.
10. For send_as="merchant_on_behalf" (customer-facing): write AS the merchant ("Dr. Meera's clinic here..."), warm + clinical, no claims, name the customer.

COMPULSION LEVERS — use 1-2 per message (these underperform in production today, lean into them):
- Specificity: cite a real number/date/source from inputs.
- Loss aversion: "missed searches", "before window closes".
- Social proof: "3 dentists in Lajpat Nagar did X this month" (ONLY if peer_stats supports it).
- Effort externalization: "I've drafted X — just say go". "5-min setup".
- Curiosity: "want to see who?" / "want the full list?"
- Reciprocity: "noticed Y about your account, thought you'd want to know".
- Ask the merchant: "what's your most-asked treatment this week?"
- Single binary commitment: "Reply YES / STOP".

KIND-SPECIFIC FRAMING:
- research_digest: lead with source citation; tie to merchant's cohort if known; offer to draft patient-ed content.
- recall_due (customer-facing): name the customer; cite months since last visit; offer 1-2 real slots from inputs; price from offer catalog.
- perf_spike: lead with the number ("views +28% yesterday"); offer to capitalise (post/offer).
- perf_dip: empathetic, diagnostic ("calls -40% w/w — likely cause: ___"); single actionable next step.
- competitor_opened: voyeur framing ("new dentist 1.3km away — quick look at how your listing compares?").
- festival_upcoming / weather_*: tie to category seasonal_beats; suggest specific copy.
- milestone_reached: celebrate concretely; ask permission to share as social proof.
- review_theme_emerged: cite the specific theme; offer to draft a response template.
- dormant_with_vera: light curiosity-ask, NOT a sales pitch; reference their last engagement.
- regulation_change: factual + source; offer to summarise impact.
- category_trend_movement: cite the % and segment; ask if they're seeing it.
- scheduled_recurring / curious_ask_due: ASK them something — "what's your most-booked service this week?".

OUTPUT — RETURN ONLY THIS JSON:
{
  "body": "<the WhatsApp message>",
  "cta": "binary_yes_stop" | "open_ended" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "template_name": "<short stable name like vera_research_digest_v1>",
  "template_params": ["param1", "param2", ...],
  "rationale": "<1-2 sentences: which compulsion levers + which inputs anchored the specifics>"
}"""


def _digest(d: Any, max_chars: int = 2500) -> str:
    """Compact JSON dump for prompt — drops nulls, trims long strings."""
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items() if v not in (None, [], {}, "")}
        if isinstance(o, list):
            return [clean(x) for x in o]
        if isinstance(o, str) and len(o) > 400:
            return o[:400] + "…"
        return o
    s = json.dumps(clean(d), ensure_ascii=False, indent=1)
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


def _build_compose_user(category: dict, merchant: dict, trigger: dict,
                        customer: Optional[dict], conv: Optional[dict]) -> str:
    languages = (merchant.get("identity") or {}).get("languages", ["en"])
    parts = [
        f"=== CATEGORY ({category.get('slug','?')}) ===",
        _digest(category),
        "",
        f"=== MERCHANT ({merchant.get('merchant_id','?')}) ===",
        _digest(merchant),
        "",
        f"=== TRIGGER ({trigger.get('kind','?')} / urgency {trigger.get('urgency','?')}) ===",
        _digest(trigger),
    ]
    if customer:
        parts += ["", f"=== CUSTOMER ({customer.get('customer_id','?')}) ===", _digest(customer)]
    if conv and conv["turns"]:
        recent = conv["turns"][-6:]
        parts += ["", "=== RECENT CONVERSATION (most-recent last) ===",
                  _digest(recent, max_chars=1500)]
    parts += [
        "",
        f"=== LANGUAGE PREF === {languages}",
        f"=== SEND_AS === {'merchant_on_behalf' if customer else 'vera'}",
        "",
        "Now produce the JSON message. Specific. Category-correct. Trigger-anchored. Engagement-compelling.",
    ]
    return "\n".join(parts)


async def compose_message(category: dict, merchant: dict, trigger: dict,
                          customer: Optional[dict], conv: Optional[dict]) -> dict:
    user = _build_compose_user(category, merchant, trigger, customer, conv)
    try:
        raw = await asyncio.wait_for(
            llm_chat(COMPOSER_SYSTEM, user, json_mode=True, temperature=0.0),
            timeout=LLM_TIMEOUT_S,
        )
        out = _parse_json(raw)
    except Exception as e:
        log.warning("compose LLM failed: %s — falling back", e)
        out = _fallback_compose(category, merchant, trigger, customer)

    out["body"] = (out.get("body") or "").strip()
    if not out["body"]:
        out = _fallback_compose(category, merchant, trigger, customer)
    out.setdefault("cta", "open_ended")
    out.setdefault("send_as", "merchant_on_behalf" if customer else "vera")
    out.setdefault("template_name", f"vera_{trigger.get('kind','generic')}_v1")
    out.setdefault("template_params", [])
    out.setdefault("rationale", "")
    return out


def _fallback_compose(category: dict, merchant: dict, trigger: dict,
                      customer: Optional[dict]) -> dict:
    name = (merchant.get("identity") or {}).get("name", "there")
    kind = trigger.get("kind", "update")
    if customer:
        cname = (customer.get("identity") or {}).get("name", "there")
        body = (f"Hi {cname}, {name} here. Just a quick note from us — "
                f"reply YES to hear more or STOP to opt out.")
        send_as = "merchant_on_behalf"
    else:
        body = (f"Hi {name}, quick {kind.replace('_',' ')} from Vera. "
                f"Want me to walk you through it? Reply YES.")
        send_as = "vera"
    return {
        "body": body,
        "cta": "binary_yes_stop",
        "send_as": send_as,
        "template_name": f"vera_{kind}_v1",
        "template_params": [name],
        "rationale": "Fallback: LLM unavailable.",
    }


AUTO_REPLY_HINTS = [
    "thank you for contacting", "thanks for contacting", "thank you for your message",
    "we will get back", "we'll get back", "will get back to you",
    "automated", "auto-reply", "auto reply", "automatic reply",
    "out of office", "currently unavailable", "currently away", "currently busy",
    "received your message", "we have received", "appreciate your patience",
    "as soon as possible", "during business hours", "will respond",
    "shukriya", "dhanyavad", "team tak pahuncha", "hamari team",
    "jaankari ke liye", "sandesh ke liye",
]
INTENT_GO_HINTS = [
    "yes", "haan", "ha ", "ok ", "okay", "go ahead", "do it", "let's do",
    "lets do", "kar do", "judrna hai", "join karna", "i want to join",
    "sure", "please proceed", "chalega", "theek hai", "thik hai", "ji haan",
]
INTENT_STOP_HINTS = [
    "stop", "not interested", "remove", "unsubscribe", "do not", "dont contact",
    "don't contact", "band karo", "mat bhejo", "nahi chahiye", "no thanks",
]
INTENT_WAIT_HINTS = [
    "later", "busy", "tomorrow", "kal ", "baad mein", "abhi nahi", "in a meeting",
    "call me later", "thodi der", "thoda time",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())

def _similar(a: str, b: str, threshold: float = 0.7) -> bool:
    """Jaccard token similarity — catches near-duplicate canned messages."""
    if not a or not b:
        return False
    sa, sb = set(a.split()), set(b.split())
    if len(sa) < 3 or len(sb) < 3:
        return False
    return len(sa & sb) / len(sa | sb) >= threshold

def detect_auto_reply(conv: dict, latest: str) -> bool:
    """Heuristic: known canned phrase, exact repeat, or fuzzy near-duplicate."""
    n = _norm(latest)
    if not n:
        return False
    for h in AUTO_REPLY_HINTS:
        if h in n:
            return True
    prior_msgs = [_norm(t["body"]) for t in conv["turns"][:-1]
                  if t["from"] in ("merchant", "customer")]
    if prior_msgs.count(n) >= 1:
        return True
    for prior in prior_msgs:
        if _similar(n, prior):
            return True
    return False


def classify_intent(text: str) -> str:
    """Returns: go | stop | wait | question | other."""
    n = _norm(text)
    if any(h in n for h in INTENT_STOP_HINTS):
        return "stop"
    if any(h in n for h in INTENT_GO_HINTS) and len(n) < 80:
        return "go"
    if any(h in n for h in INTENT_WAIT_HINTS):
        return "wait"
    if "?" in text or n.startswith(("what", "kya", "how", "kaise", "kab", "when", "kyun", "why", "kaun", "who")):
        return "question"
    return "other"


REPLY_SYSTEM = """You are VERA continuing an active WhatsApp conversation. The MERCHANT (or CUSTOMER) just replied. Decide the next move.

You receive: full context (category/merchant/trigger/optional customer), conversation so far, the latest reply, and a pre-classified intent hint.

DECIDE one of:
- "send"  → produce the next message body + cta
- "wait"  → back off N seconds (use when merchant asked for time)
- "end"   → close the conversation gracefully (auto-reply detected, hard no, or natural end)

KEY ROUTING:
- intent=go   → SWITCH FROM PITCH/QUALIFICATION TO ACTION. Don't ask another qualifying question. Confirm + do the thing.
- intent=stop → action="end" with one-line polite exit ("Samajh gayi, all the best!").
- intent=wait → action="wait" with wait_seconds (1800 = 30min, 7200 = 2h, 86400 = next day).
- intent=question → answer concretely from context; don't deflect.
- auto_reply=true (≥2 strikes) → action="end" with polite exit. Don't waste another turn.
- intent=other / unclear → if engaged, advance the agenda with ONE concrete next step + binary CTA. If 3rd unanswered nudge, end.

ABSOLUTE RULES:
- NEVER repeat a body verbatim from earlier in this conversation.
- NEVER invent facts.
- Honor language pref. Match the language the merchant just used if they switched.
- Concise. Single CTA. No re-introducing.

OUTPUT JSON (only these keys for the chosen action):
For send: {"action":"send","body":"...","cta":"binary_yes_stop"|"open_ended"|"none","rationale":"..."}
For wait: {"action":"wait","wait_seconds":<int>,"rationale":"..."}
For end:  {"action":"end","rationale":"...", "body":"<optional final one-line goodbye>"}"""


def _build_reply_user(category: dict, merchant: dict, trigger: dict,
                      customer: Optional[dict], conv: dict,
                      latest: str, intent: str, auto_reply: bool) -> str:
    parts = [
        f"=== CATEGORY ===\n{_digest(category, 1500)}",
        f"=== MERCHANT ===\n{_digest(merchant, 1800)}",
        f"=== TRIGGER ===\n{_digest(trigger, 800)}",
    ]
    if customer:
        parts.append(f"=== CUSTOMER ===\n{_digest(customer, 800)}")
    parts += [
        f"=== CONVERSATION SO FAR ===\n{_digest(conv['turns'], 1800)}",
        f"=== LATEST REPLY ===\n{latest}",
        f"=== HINT === intent={intent}, auto_reply={auto_reply}, "
        f"auto_reply_strikes={conv['auto_reply_strikes']}, turn={len(conv['turns'])}",
        "",
        "Decide next move. Return JSON only.",
    ]
    return "\n".join(parts)

def _fallback_reply(intent: str, merchant: dict, trigger: dict,
                    customer: Optional[dict]) -> dict:
    """Deterministic reply when the LLM is unavailable — intent-aware."""
    name = (merchant.get("identity") or {}).get("name", "there")
    offer = ""
    offers = merchant.get("offers") or []
    active = [o for o in offers if o.get("status") == "active"]
    if active:
        offer = active[0].get("title", "")

    if intent == "go":
        body = (f"Done — kicking off now. "
                + (f"Will draft around your active '{offer}' offer. " if offer else "")
                + "I'll share the draft within a few minutes. Reply YES to publish or STOP to review first.")
        return {"action": "send", "body": body, "cta": "binary_yes_stop",
                "rationale": "Fallback (LLM unavailable): honoring 'go' intent — confirming + advancing."}

    if intent == "wait":
        return {"action": "wait", "wait_seconds": 7200,
                "rationale": "Fallback: merchant asked for time — backing off 2h."}

    if intent == "question":
        return {"action": "send",
                "body": "Good question — let me pull the exact details and circle back in a minute. Reply STOP if you'd rather not.",
                "cta": "binary_yes_stop",
                "rationale": "Fallback: question intent — acknowledging without making up an answer."}

    return {"action": "send",
            "body": "Got it — quick yes/no: should I go ahead with the next step? Reply YES or STOP.",
            "cta": "binary_yes_stop",
            "rationale": "Fallback: ambiguous intent — single binary CTA."}


async def compose_reply(category: dict, merchant: dict, trigger: dict,
                        customer: Optional[dict], conv: dict,
                        latest: str) -> dict:
    auto = detect_auto_reply(conv, latest)
    intent = classify_intent(latest)
    mid = conv.get("merchant_id", "")
    if intent == "stop":
        merchant_auto_strikes[mid] = 0
        return {"action": "end",
                "body": "Samajh gayi, no more messages from my side. All the best!",
                "rationale": "Hard not-interested signal — graceful exit."}
    if intent == "go":
        merchant_auto_strikes[mid] = 0
        conv["auto_reply_strikes"] = 0
    if auto and intent != "go":
        conv["auto_reply_strikes"] += 1
        merchant_auto_strikes[mid] += 1
    strikes = max(conv["auto_reply_strikes"], merchant_auto_strikes.get(mid, 0))
    if strikes >= 2 and intent != "go":
        return {"action": "end",
                "body": "Koi baat nahi, samajh gayi. Owner/manager se directly connect kar lungi. Aapka business accha chal raha hai — best wishes! 🙂",
                "rationale": f"Auto-reply detected {strikes}× — graceful exit (Pattern B)."}
    if auto and strikes == 1 and intent != "go":
        return {"action": "send",
                "body": "Samajh gayi — automated reply lag raha hai. 30 second laga ke khud check kar sakti hain? Main wait kar sakti hoon.",
                "cta": "binary_yes_stop",
                "rationale": "Auto-reply strike 1 — one polite probe (Pattern B step 1)."}
    user = _build_reply_user(category, merchant, trigger, customer, conv,
                             latest, intent, auto)
    try:
        raw = await asyncio.wait_for(
            llm_chat(REPLY_SYSTEM, user, json_mode=True, temperature=0.0),
            timeout=LLM_TIMEOUT_S,
        )
        out = _parse_json(raw)
    except Exception as e:
        log.warning("reply LLM failed: %s — falling back", e)
        out = _fallback_reply(intent, merchant, trigger, customer)

    if out.get("action") == "send":
        body = (out.get("body") or "").strip()
        prior_bot = {_norm(t["body"]) for t in conv["turns"] if t["from"] == "vera"}
        if not body or _norm(body) in prior_bot:
            out = {"action": "wait", "wait_seconds": 3600,
                   "rationale": "Anti-repetition: backing off rather than repeat self."}
    return out


def rank_triggers(merchant_id: str, trigger_ids: list[str]) -> list[str]:
    """Pick best triggers to fire this tick: highest urgency, not yet fired, not suppressed."""
    scored: list[tuple[int, str]] = []
    for tid in trigger_ids:
        trg = _ctx("trigger", tid)
        if not trg:
            continue
        if trg.get("merchant_id") and trg["merchant_id"] != merchant_id:
            continue
        if (merchant_id, tid) in trigger_fired:
            continue
        sk = trg.get("suppression_key")
        if sk and (merchant_id, sk) in fired:
            continue
        exp = trg.get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(exp.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                    continue
            except Exception:
                pass
        urgency = int(trg.get("urgency") or 1)
        scored.append((urgency, tid))
    scored.sort(reverse=True)
    return [tid for _, tid in scored]


class CtxBody(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok",
            "uptime_seconds": int(time.time() - START_TS),
            "contexts_loaded": counts,
            "active_conversations": sum(1 for c in conversations.values() if c["status"] == "active")}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": f"{LLM_PROVIDER}:{LLM_MODEL}",
        "approach": APPROACH,
        "contact_email": CONTACT_EMAIL,
        "version": VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {
        "version":   body.version,
        "payload":   body.payload,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"accepted": True,
            "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": contexts[key]["stored_at"]}


@app.post("/v1/tick")
async def tick(body: TickBody):
    """For each active trigger we haven't handled, compose & emit at most ONE action per merchant."""
    actions: list[dict] = []
    handled_merchants: set[str] = set()

    by_merchant: dict[str, list[str]] = defaultdict(list)
    for tid in body.available_triggers:
        trg = _ctx("trigger", tid)
        if not trg:
            continue
        mid = trg.get("merchant_id")
        if mid:
            by_merchant[mid].append(tid)

    async def _one(mid: str, tid: str) -> Optional[dict]:
        trigger = _ctx("trigger", tid)
        merchant = _ctx("merchant", mid)
        if not (trigger and merchant):
            return None
        category = _ctx("category", merchant.get("category_slug"))
        if not category:
            return None
        cust_id = trigger.get("customer_id")
        customer = _ctx("customer", cust_id) if cust_id else None
        try:
            msg = await compose_message(category, merchant, trigger, customer, conv=None)
        except Exception as e:
            log.exception("compose error: %s", e)
            return None

        cid = f"conv_{mid}_{tid}"
        conv = _new_conv(cid, mid, cust_id, tid, msg["send_as"])
        conv["turns"].append({"from": "vera", "body": msg["body"],
                              "ts": body.now})
        conv["last_bot_body"] = msg["body"]

        trigger_fired[(mid, tid)] = cid
        sk = trigger.get("suppression_key")
        if sk:
            fired[(mid, sk)] = body.now

        return {
            "conversation_id": cid,
            "merchant_id":     mid,
            "customer_id":     cust_id,
            "send_as":         msg["send_as"],
            "trigger_id":      tid,
            "template_name":   msg.get("template_name", "vera_generic_v1"),
            "template_params": msg.get("template_params", []),
            "body":            msg["body"],
            "cta":             msg.get("cta", "open_ended"),
            "suppression_key": sk or "",
            "rationale":       msg.get("rationale", ""),
        }

    tasks = []
    for mid, tids in by_merchant.items():
        if mid in handled_merchants:
            continue
        ranked = rank_triggers(mid, tids)
        if not ranked:
            continue
        handled_merchants.add(mid)
        tasks.append(_one(mid, ranked[0]))   
        if len(tasks) >= 18:                 
            break

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict):
                actions.append(r)

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = conversations.get(body.conversation_id)
    if not conv:
        mid = body.merchant_id or ""
        cust_id = body.customer_id
        send_as = "merchant_on_behalf" if cust_id else "vera"
        conv = _new_conv(
            cid=body.conversation_id,
            merchant_id=mid,
            customer_id=cust_id,
            trigger_id="<unknown>",
            send_as=send_as,
        )
        log.info("bootstrapped unknown conversation %s for merchant=%s",
                 body.conversation_id, mid)

    conv["turns"].append({"from": body.from_role,
                          "body": body.message,
                          "ts":   body.received_at})

    merchant = _ctx("merchant", conv["merchant_id"]) or {}
    category = _ctx("category", merchant.get("category_slug")) or {}
    trigger  = _ctx("trigger", conv["trigger_id"]) or {
        "kind": "ad_hoc_reply", "scope": "merchant", "source": "internal",
        "urgency": 2, "payload": {},
    }
    customer = _ctx("customer", conv["customer_id"]) if conv["customer_id"] else None

    try:
        out = await asyncio.wait_for(
            compose_reply(category, merchant, trigger, customer, conv, body.message),
            timeout=LLM_TIMEOUT_S + 4,
        )
    except Exception as e:
        log.exception("reply error: %s", e)
        out = {"action": "wait", "wait_seconds": 1800,
               "rationale": "Reply pipeline error — backing off."}

    if out.get("action") == "send" and out.get("body"):
        conv["turns"].append({"from": "vera",
                              "body": out["body"],
                              "ts":   datetime.now(timezone.utc).isoformat()})
        conv["last_bot_body"] = out["body"]
    elif out.get("action") == "end":
        conv["status"] = "ended"
        if out.get("body"):
            conv["turns"].append({"from": "vera", "body": out["body"],
                                  "ts": datetime.now(timezone.utc).isoformat()})

    return out


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    fired.clear()
    trigger_fired.clear()
    merchant_convs.clear()
    return {"ok": True}