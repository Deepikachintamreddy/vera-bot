"""
Local smoke test — exercises all 5 endpoints against your running bot.

Usage:
    1. In one terminal:  uvicorn app:app --host 0.0.0.0 --port 7860 --reload
    2. In another:       python test_local.py

Expected output:
    [PASS] healthz, metadata, push 5 categories, push 10 merchants, push 25
    triggers, tick returns N actions, reply handles auto-reply / intent /
    hostile correctly.

This does NOT score messages (use the official judge_simulator.py for that).
It just verifies the endpoint contract is correct so you don't fail warmup.
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

BOT_URL = os.environ.get("BOT_URL", "http://localhost:7860")
DATASET = Path(__file__).parent / "dataset"   # assumes dataset/ sits next to this file


def green(s): return f"\033[92m{s}\033[0m"
def red(s): return f"\033[91m{s}\033[0m"
def cyan(s): return f"\033[96m{s}\033[0m"
def dim(s): return f"\033[2m{s}\033[0m"


def header(s):
    print(f"\n{cyan('━' * 60)}")
    print(cyan(f"  {s}"))
    print(cyan('━' * 60))


def check(label, condition, detail=""):
    if condition:
        print(f"  {green('PASS')}  {label} {dim(detail)}")
        return True
    else:
        print(f"  {red('FAIL')}  {label} {dim(detail)}")
        return False


def load_dataset():
    """Load the 5 categories, 10 merchants, 25 triggers from the seeds."""
    if not DATASET.exists():
        print(red(f"Dataset folder not found at {DATASET}"))
        print(dim("Copy the challenge dataset/ folder next to this script."))
        sys.exit(1)

    cats = {}
    for f in (DATASET / "categories").glob("*.json"):
        c = json.loads(f.read_text(encoding="utf-8"))
        cats[c["slug"]] = c

    merchants_seed = json.loads((DATASET / "merchants_seed.json").read_text(encoding="utf-8"))
    merchants = {m["merchant_id"]: m for m in merchants_seed.get("merchants", [])}

    triggers_seed = json.loads((DATASET / "triggers_seed.json").read_text(encoding="utf-8"))
    triggers = {t["id"]: t for t in triggers_seed.get("triggers", [])}

    customers_seed = json.loads((DATASET / "customers_seed.json").read_text(encoding="utf-8"))
    customers = {c["customer_id"]: c for c in customers_seed.get("customers", [])}

    return cats, merchants, customers, triggers


def main():
    print(header("Vera Bot Local Smoke Test"))
    print(f"  Bot URL: {BOT_URL}")

    client = httpx.Client(timeout=30.0)

    # ----- /v1/healthz -----
    header("Phase 1: Health & Metadata")
    r = client.get(f"{BOT_URL}/v1/healthz")
    check("healthz returns 200", r.status_code == 200, f"{r.status_code}")
    check("healthz has contexts_loaded", "contexts_loaded" in r.json())

    r = client.get(f"{BOT_URL}/v1/metadata")
    check("metadata returns 200", r.status_code == 200)
    if r.status_code == 200:
        m = r.json()
        check("metadata has team_name", bool(m.get("team_name")), m.get("team_name", ""))
        check("metadata has model", bool(m.get("model")), m.get("model", ""))

    # ----- Load + push contexts -----
    header("Phase 2: Context Push")
    cats, merchants, customers, triggers = load_dataset()

    n_ok = 0
    for slug, cat in cats.items():
        r = client.post(f"{BOT_URL}/v1/context", json={
            "scope": "category", "context_id": slug, "version": 1,
            "payload": cat, "delivered_at": "2026-04-29T00:00:00Z"
        })
        if r.status_code == 200 and r.json().get("accepted"):
            n_ok += 1
    check(f"Pushed {len(cats)} categories", n_ok == len(cats), f"{n_ok}/{len(cats)}")

    n_ok = 0
    for mid, m in merchants.items():
        r = client.post(f"{BOT_URL}/v1/context", json={
            "scope": "merchant", "context_id": mid, "version": 1,
            "payload": m, "delivered_at": "2026-04-29T00:00:00Z"
        })
        if r.status_code == 200 and r.json().get("accepted"):
            n_ok += 1
    check(f"Pushed {len(merchants)} merchants", n_ok == len(merchants), f"{n_ok}/{len(merchants)}")

    n_ok = 0
    for cid, c in customers.items():
        r = client.post(f"{BOT_URL}/v1/context", json={
            "scope": "customer", "context_id": cid, "version": 1,
            "payload": c, "delivered_at": "2026-04-29T00:00:00Z"
        })
        if r.status_code == 200 and r.json().get("accepted"):
            n_ok += 1
    check(f"Pushed {len(customers)} customers", n_ok == len(customers), f"{n_ok}/{len(customers)}")

    n_ok = 0
    for tid, t in triggers.items():
        r = client.post(f"{BOT_URL}/v1/context", json={
            "scope": "trigger", "context_id": tid, "version": 1,
            "payload": t, "delivered_at": "2026-04-29T00:00:00Z"
        })
        if r.status_code == 200 and r.json().get("accepted"):
            n_ok += 1
    check(f"Pushed {len(triggers)} triggers", n_ok == len(triggers), f"{n_ok}/{len(triggers)}")

    # ----- Idempotency check -----
    first_trg = next(iter(triggers))
    r = client.post(f"{BOT_URL}/v1/context", json={
        "scope": "trigger", "context_id": first_trg, "version": 1,
        "payload": triggers[first_trg]
    })
    body = r.json()
    check("Re-pushing same version returns stale_version",
          (not body.get("accepted")) and body.get("reason") == "stale_version")

    # ----- /v1/healthz reflects pushed contexts -----
    r = client.get(f"{BOT_URL}/v1/healthz")
    cl = r.json().get("contexts_loaded", {})
    check("healthz contexts_loaded reflects pushes",
          cl.get("category", 0) == len(cats) and cl.get("merchant", 0) == len(merchants),
          f"{cl}")

    # ----- /v1/tick -----
    header("Phase 3: Tick (LLM composition)")
    test_trgs = list(triggers.keys())[:3]
    print(f"  Sending tick with triggers: {test_trgs}")
    t0 = time.time()
    r = client.post(f"{BOT_URL}/v1/tick", json={
        "now": "2026-04-29T10:00:00Z",
        "available_triggers": test_trgs,
    }, timeout=60.0)
    elapsed = time.time() - t0
    check(f"tick returns 200 ({elapsed:.1f}s)", r.status_code == 200)

    if r.status_code == 200:
        actions = r.json().get("actions", [])
        check(f"tick returned {len(actions)} action(s)", len(actions) > 0)
        for i, a in enumerate(actions):
            print(f"\n  {cyan('Action ' + str(i+1))}:")
            print(f"    trigger:    {a.get('trigger_id')}")
            print(f"    merchant:   {a.get('merchant_id')}")
            print(f"    send_as:    {a.get('send_as')}")
            print(f"    cta:        {a.get('cta')}")
            print(f"    body:       {a.get('body', '')[:160]}{'...' if len(a.get('body', '')) > 160 else ''}")
            print(f"    rationale:  {a.get('rationale', '')[:120]}")

    # ----- /v1/reply: auto-reply -----
    header("Phase 4: Reply — Auto-reply detection")
    auto = "Thank you for contacting us! Our team will respond shortly."
    for i in range(3):
        r = client.post(f"{BOT_URL}/v1/reply", json={
            "conversation_id": "conv_smoke_auto",
            "merchant_id": next(iter(merchants)),
            "from_role": "merchant", "message": auto,
            "received_at": "2026-04-29T10:01:00Z", "turn_number": i + 1,
        })
        action = r.json().get("action")
        print(f"  Turn {i+1}: action={action}")
        if action == "end":
            check(f"Bot ended on auto-reply turn {i+1}", True)
            break
    else:
        check("Bot ended on auto-reply within 3 turns", False)

    # ----- /v1/reply: intent transition -----
    header("Phase 5: Reply — Intent transition")
    r = client.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": "conv_smoke_intent",
        "merchant_id": next(iter(merchants)),
        "from_role": "merchant",
        "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-29T10:02:00Z", "turn_number": 2,
    })
    body = r.json().get("body", "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["sending", "drafted", "draft", "here", "confirm", "proceed", "next", "done"]
    is_actioning = any(w in body for w in actioning) and not any(w in body for w in qualifying)
    print(f"  Reply body: {body[:200]}")
    check("Bot switched to ACTION mode (no further qualifying)", is_actioning)

    # ----- /v1/reply: hostile -----
    header("Phase 6: Reply — Hostile handling")
    r = client.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": "conv_smoke_hostile",
        "merchant_id": next(iter(merchants)),
        "from_role": "merchant",
        "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-04-29T10:03:00Z", "turn_number": 2,
    })
    j = r.json()
    print(f"  Reply: action={j.get('action')}, body={j.get('body', '')[:120]}")
    ok = j.get("action") == "end" or "sorry" in j.get("body", "").lower()
    check("Bot exits or apologizes on hostile message", ok)

    print()
    print(green("Smoke test complete. Now run the official judge_simulator.py for scoring."))


if __name__ == "__main__":
    main()
