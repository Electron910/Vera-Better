"""Quick local smoke test — fires fixtures at the bot to verify all 5 endpoints."""
import asyncio, json, httpx
from datetime import datetime, timezone

BOT = "https://vera-better.onrender.com"
NOW = datetime.now(timezone.utc).isoformat()

CATEGORY = {
    "slug": "dentists",
    "voice": {"tone": "peer_clinical", "vocab_allowed": ["fluoride varnish", "caries"],
              "taboos": ["cure", "guaranteed"]},
    "peer_stats": {"avg_rating": 4.4, "avg_reviews": 62, "avg_ctr": 0.030,
                   "scope": "delhi_solo_practices"},
    "offer_catalog": [
        {"title": "Dental Cleaning @ ₹299", "value": "299", "audience": "new_user"},
        {"title": "Free Consultation", "value": "0", "audience": "new_user"},
        {"title": "Teeth Whitening @ ₹1,499", "value": "1499", "audience": "all"},
    ],
    "digest": [{
        "id": "d_2026W17_jida_fluoride", "kind": "research",
        "title": "3-mo fluoride recall cuts caries 38% better than 6-mo",
        "source": "JIDA Oct 2026, p.14", "trial_n": 2100,
        "patient_segment": "high_risk_adults",
        "summary": "RCT showing 3-month vs 6-month fluoride recall in high-risk adults."
    }],
    "patient_content_library": [
        {"id": "pc_001", "title": "3 things your teeth tell you about your heart",
         "channel": "whatsapp", "body": "Short patient-friendly summary..."}
    ],
    "seasonal_beats": [{"month_range": "Nov-Feb", "note": "exam-stress bruxism spike"}],
    "trend_signals": [{"query": "clear aligners delhi", "delta_yoy": 0.62,
                       "segment_age": "28-45"}],
}

MERCHANT = {
    "merchant_id": "m_001_drmeera",
    "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi",
                 "locality": "Lajpat Nagar", "place_id": "ChIJxxx",
                 "verified": True, "languages": ["en", "hi"]},
    "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
    "performance": {"window_days": 30, "views": 2410, "calls": 18,
                    "directions": 45, "ctr": 0.021,
                    "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05}},
    "offers": [
        {"id": "o_001", "title": "Dental Cleaning @ ₹299", "status": "active"},
        {"id": "o_002", "title": "Deep Cleaning @ ₹499", "status": "expired"},
    ],
    "conversation_history": [
        {"ts": "2026-04-24T10:00:00Z", "from": "vera",
         "body": "Hi Dr. Meera, quick check-in...", "engagement": "merchant_replied"}
    ],
    "customer_aggregate": {"total_unique_ytd": 540, "lapsed_180d_plus": 78,
                           "retention_6mo_pct": 0.38},
    "signals": ["stale_posts:22d", "ctr_below_peer_median", "high_risk_adult_cohort"],
}

CUSTOMER = {
    "customer_id": "c_001_priya",
    "merchant_id": "m_001_drmeera",
    "identity": {"name": "Priya", "phone_redacted": "<phone>",
                 "language_pref": "hi-en mix"},
    "relationship": {"first_visit": "2025-11-04", "last_visit": "2025-11-04",
                     "visits_total": 4,
                     "services_received": ["cleaning", "cleaning", "whitening", "cleaning"]},
    "state": "lapsed_soft",
    "preferences": {"preferred_slots": "weekday_evening", "channel": "whatsapp"},
    "consent": {"opted_in_at": "2025-11-04",
                "scope": ["recall_reminders", "appointment_reminders"]},
}

TRIGGER_DIGEST = {
    "id": "trg_digest_dentists",
    "scope": "merchant", "kind": "research_digest", "source": "external",
    "merchant_id": "m_001_drmeera", "customer_id": None,
    "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
    "urgency": 2, "suppression_key": "research:dentists:2026-W17",
    "expires_at": "2026-12-31T00:00:00Z",
}

TRIGGER_RECALL = {
    "id": "trg_recall_priya",
    "scope": "customer", "kind": "recall_due", "source": "internal",
    "merchant_id": "m_001_drmeera", "customer_id": "c_001_priya",
    "payload": {"patient_id": "c_001_priya", "last_visit": "2025-11-04",
                "due_date": "2026-05-04"},
    "urgency": 3, "suppression_key": "recall:c_001_priya",
    "expires_at": "2026-12-31T00:00:00Z",
}


async def push(cli, scope, ctx_id, payload, version=1):
    r = await cli.post(f"{BOT}/v1/context", json={
        "scope": scope, "context_id": ctx_id, "version": version,
        "payload": payload, "delivered_at": NOW,
    })
    print(f"  push {scope}/{ctx_id} v{version} → {r.status_code} {r.json()}")


async def main():
    async with httpx.AsyncClient(timeout=60) as cli:
        print("\n[1] /v1/healthz")
        r = await cli.get(f"{BOT}/v1/healthz")
        print(" ", r.json())

        print("\n[2] /v1/metadata")
        r = await cli.get(f"{BOT}/v1/metadata")
        print(" ", r.json())

        print("\n[3] /v1/context (push fixtures)")
        await push(cli, "category", "dentists", CATEGORY)
        await push(cli, "merchant", "m_001_drmeera", MERCHANT)
        await push(cli, "customer", "c_001_priya", CUSTOMER)
        await push(cli, "trigger",  "trg_digest_dentists", TRIGGER_DIGEST)
        await push(cli, "trigger",  "trg_recall_priya",    TRIGGER_RECALL)

        print("\n[3b] idempotency check (re-push v1 → should be stale)")
        await push(cli, "merchant", "m_001_drmeera", MERCHANT, version=1)

        print("\n[3c] version bump (v2 → should be accepted)")
        m2 = {**MERCHANT,
              "performance": {**MERCHANT["performance"], "views": 3100,
                              "delta_7d": {"views_pct": 0.28, "calls_pct": 0.10}}}
        await push(cli, "merchant", "m_001_drmeera", m2, version=2)

        print("\n[4] /v1/tick (merchant-facing digest)")
        r = await cli.post(f"{BOT}/v1/tick", json={
            "now": NOW, "available_triggers": ["trg_digest_dentists"],
        })
        actions = r.json().get("actions", [])
        print(f"  {len(actions)} action(s):")
        for a in actions:
            print(f"  → {a['conversation_id']} (send_as={a['send_as']}, cta={a['cta']})")
            print(f"    body: {a['body']}")
            print(f"    rationale: {a['rationale']}")

        print("\n[5] /v1/tick (customer-facing recall)")
        r = await cli.post(f"{BOT}/v1/tick", json={
            "now": NOW, "available_triggers": ["trg_recall_priya"],
        })
        actions2 = r.json().get("actions", [])
        for a in actions2:
            print(f"  → {a['conversation_id']} (send_as={a['send_as']}, cta={a['cta']})")
            print(f"    body: {a['body']}")

        print("\n[6] /v1/reply — engaged 'go' intent")
        if actions:
            cid = actions[0]["conversation_id"]
            r = await cli.post(f"{BOT}/v1/reply", json={
                "conversation_id": cid, "merchant_id": "m_001_drmeera",
                "from_role": "merchant",
                "message": "Yes, please send me the abstract and the patient draft",
                "received_at": NOW, "turn_number": 2,
            })
            print(" ", json.dumps(r.json(), ensure_ascii=False, indent=2))

        print("\n[7] /v1/reply — auto-reply detection (canned)")
        if actions2:
            cid = actions2[0]["conversation_id"]
            canned = ("Aapki jaankari ke liye bahut-bahut shukriya. "
                      "Main aapki yeh sabhi baatein aur sujhaav hamari team tak pahuncha deti hoon.")
            r = await cli.post(f"{BOT}/v1/reply", json={
                "conversation_id": cid, "merchant_id": "m_001_drmeera",
                "customer_id": "c_001_priya",
                "from_role": "merchant", "message": canned,
                "received_at": NOW, "turn_number": 2,
            })
            print("  strike 1 →", json.dumps(r.json(), ensure_ascii=False, indent=2))

            r = await cli.post(f"{BOT}/v1/reply", json={
                "conversation_id": cid, "merchant_id": "m_001_drmeera",
                "customer_id": "c_001_priya",
                "from_role": "merchant", "message": canned,
                "received_at": NOW, "turn_number": 3,
            })
            print("  strike 2 →", json.dumps(r.json(), ensure_ascii=False, indent=2))

        print("\n[8] /v1/reply — hard stop")
        if actions:
            cid = actions[0]["conversation_id"]
            r = await cli.post(f"{BOT}/v1/reply", json={
                "conversation_id": cid, "merchant_id": "m_001_drmeera",
                "from_role": "merchant",
                "message": "Not interested, please remove me",
                "received_at": NOW, "turn_number": 3,
            })
            print(" ", json.dumps(r.json(), ensure_ascii=False, indent=2))

        print("\n[9] empty tick (restraint)")
        r = await cli.post(f"{BOT}/v1/tick", json={"now": NOW, "available_triggers": []})
        print(" ", r.json())

        print("\n[10] healthz after run")
        r = await cli.get(f"{BOT}/v1/healthz")
        print(" ", r.json())

    print("\n✅ smoke test complete")


if __name__ == "__main__":
    asyncio.run(main())