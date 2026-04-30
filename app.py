"""
Vera Bot — magicpin AI Challenge submission.

FastAPI server exposing the 5 required endpoints:
  GET  /v1/healthz       — liveness + context counts
  GET  /v1/metadata      — team identity
  POST /v1/context       — idempotent context push (category/merchant/customer/trigger)
  POST /v1/tick          — periodic wake-up; bot decides what to send
  POST /v1/reply         — handles inbound reply from simulated merchant/customer
  POST /v1/teardown      — optional, wipes state on test end

Run:  uvicorn app:app --host 0.0.0.0 --port 7860
"""
import os
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager

import llm
import composer
import state


# =============================================================================
# Identity (fill in your details)
# =============================================================================
TEAM_NAME = os.environ.get("TEAM_NAME", "GrowthGenie")
TEAM_MEMBERS = [s.strip() for s in os.environ.get(
    "TEAM_MEMBERS", "Deepika Chintamreddy").split(",") if s.strip()]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "deepika@example.com")
BOT_VERSION = "1.1.0"
SUBMITTED_AT = "2026-04-29T00:00:00Z"


# =============================================================================
# In-memory + disk-backed stores (HF Space restart insurance)
# =============================================================================

# Where we persist state. /tmp survives within a Space session (not across
# rebuilds, but neither does the in-memory store). The cost of writing on
# every context push is ~1ms — negligible compared to the 30s tick budget.
PERSIST_DIR = Path(os.environ.get("VERA_PERSIST_DIR", "/tmp/vera-state"))
PERSIST_DIR.mkdir(parents=True, exist_ok=True)
CONTEXTS_FILE = PERSIST_DIR / "contexts.json"

# (scope, context_id) → {version: int, payload: dict}
contexts: dict[tuple[str, str], dict] = {}
# merchant_id → last_contact_unix_ts (used by composer for decision-quality)
recent_contacts: dict[str, float] = {}

conv_store = state.ConversationStore()
START_TS = time.time()


def _persist_contexts():
    """Write contexts to disk. Called after every successful context push.
    Costs ~1-3ms for 255 contexts. Survives uvicorn worker restart within
    the same Space session."""
    try:
        # Keys are tuples — JSON needs strings
        serial = {f"{scope}::{cid}": v for (scope, cid), v in contexts.items()}
        CONTEXTS_FILE.write_text(json.dumps(serial), encoding="utf-8")
        print(f"[persist] wrote {len(contexts)} contexts to disk")
    except Exception as e:
        # Persistence is best-effort; don't block the response on disk failure
        print(f"[persist] write failed: {e}")


def _restore_contexts():
    """Restore contexts from disk on startup. Silent if file doesn't exist."""
    try:
        if not CONTEXTS_FILE.exists():
            return
        serial = json.loads(CONTEXTS_FILE.read_text(encoding="utf-8"))
        for k, v in serial.items():
            scope, _, cid = k.partition("::")
            if scope and cid:
                contexts[(scope, cid)] = v
        if contexts:
            print(f"[persist] restored {len(contexts)} contexts from disk")
    except Exception as e:
        print(f"[persist] restore failed (continuing fresh): {e}")


# =============================================================================
# App lifecycle (clean shutdown of HTTP client)
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    _restore_contexts()
    yield
    await llm.shutdown()


app = FastAPI(title="Vera Bot", version=BOT_VERSION, lifespan=lifespan)


# =============================================================================
# /v1/healthz
# =============================================================================

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TS),
        "contexts_loaded": counts,
    }


# Friendly root for HF Spaces — keeps the Space discoverable and prevents 404s
# when someone visits the public URL in a browser.
@app.get("/")
async def root():
    return {
        "service": "Vera Bot — magicpin AI Challenge",
        "team": TEAM_NAME,
        "version": BOT_VERSION,
        "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context",
                      "/v1/tick", "/v1/reply"],
    }


# =============================================================================
# /v1/metadata
# =============================================================================

@app.get("/v1/metadata")
async def metadata():
    primary_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": primary_model,
        "approach": (
            "4-context composer with explicit decision-quality logic: "
            "trigger prioritization (urgency desc, internal-before-external, "
            "high-urgency-only filter, per-merchant dedupe, recent-contact "
            "demotion), per-family prompt dispatch across 8 trigger families, "
            "post-LLM hallucination + language + CTA + taboo validator with "
            "single re-prompt on failure, multi-turn reply state machine "
            "(auto-reply detection / intent transitions / hostile exits), and "
            "disk-backed context persistence as restart insurance."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": SUBMITTED_AT,
    }


# =============================================================================
# /v1/context — idempotent context push
# =============================================================================

class ContextBody(BaseModel):
    scope: str            # "category" | "merchant" | "customer" | "trigger"
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return {"accepted": False, "reason": "invalid_scope",
                "details": f"scope must be one of {sorted(VALID_SCOPES)}"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version",
                "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    # Persist to disk so we survive HF Space worker restarts mid-test
    print(f"[context] stored {body.scope}:{body.context_id} v{body.version}, now {len(contexts)} total")
    _persist_contexts()
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z",
    }


# =============================================================================
# /v1/tick — periodic; bot decides which triggers to act on
# =============================================================================

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


def _get_payload(scope: str, cid: str) -> Optional[dict]:
    entry = contexts.get((scope, cid))
    return entry["payload"] if entry else None


def _resolve_category_for_merchant(merchant: dict) -> Optional[dict]:
    slug = merchant.get("category_slug")
    return _get_payload("category", slug) if slug else None


@app.post("/v1/tick")
async def tick(body: TickBody):
    items = []
    print(f"[tick] triggers={body.available_triggers} keys={list(contexts.keys())}")
    for trg_id in (body.available_triggers or []):
        trg = _get_payload("trigger", trg_id)
        if not trg:
            continue
        merchant_id = trg.get("merchant_id")
        if not merchant_id:
            continue
        merchant = _get_payload("merchant", merchant_id)
        if not merchant:
            continue
        category = _resolve_category_for_merchant(merchant)
        if not category:
            continue

        customer = None
        if trg.get("scope") == "customer":
            cid = trg.get("customer_id")
            if cid:
                customer = _get_payload("customer", cid)
                # If customer trigger is missing the customer context, we'll
                # still try (the LLM can compose a relationship-light recall).

        # conversation_id encodes the trigger so the judge can resume
        conv_id = f"conv_{merchant_id}_{trg_id}"

        items.append({
            "category": category,
            "merchant": merchant,
            "trigger": trg,
            "customer": customer,
            "conversation_id": conv_id,
        })

    if not items:
        return {"actions": []}

    # Decision-quality triage is fully delegated to composer.compose_many:
    #   - drop already-sent (suppression_key dedupe)
    #   - drop expired
    #   - sort by urgency desc, internal-before-external
    #   - per-merchant: keep only highest-priority trigger
    #   - if high-urgency exists, drop urgency 1 noise
    #   - demote merchants contacted in last 4 hours unless urgency >= 4
    #   - cap at 5 messages per tick
    actions = await composer.compose_many(items, recent_contacts=recent_contacts)

    # Record outbound + update recent_contacts so the NEXT tick demotes these
    # merchants if their next trigger is low-urgency
    now_ts = time.time()
    for a in actions:
        conv_store.record_outbound(
            conv_id=a["conversation_id"],
            body=a.get("body", ""),
            merchant_id=a.get("merchant_id"),
            customer_id=a.get("customer_id"),
        )
        mid = a.get("merchant_id")
        if mid:
            recent_contacts[mid] = now_ts

    return {"actions": actions}


# =============================================================================
# /v1/reply — handle inbound merchant/customer message
# =============================================================================

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    # Record inbound first
    conv = conv_store.record_inbound(body.conversation_id, body.message,
                                     from_role=body.from_role)
    if body.merchant_id and not conv.merchant_id:
        conv.merchant_id = body.merchant_id
    if body.customer_id and not conv.customer_id:
        conv.customer_id = body.customer_id

    # If conversation is already ended, don't talk further
    if conv.ended:
        return {"action": "end",
                "rationale": f"Conversation previously ended ({conv.ended_reason})."}

    # Hostile path — handled inside reply_for; same for auto-reply.
    merchant_ctx = _get_payload("merchant", conv.merchant_id) if conv.merchant_id else None
    category_ctx = _resolve_category_for_merchant(merchant_ctx) if merchant_ctx else None
    customer_ctx = _get_payload("customer", conv.customer_id) if conv.customer_id else None

    result = await composer.reply_for(conv, body.message, merchant_ctx,
                                      category_ctx, customer_ctx)

    # If we sent something, record it in the conversation
    if result.get("action") == "send":
        conv_store.record_outbound(body.conversation_id, result.get("body", ""))
    elif result.get("action") == "end":
        conv_store.end(body.conversation_id, result.get("rationale", ""))

    return result


# =============================================================================
# /v1/teardown — optional, wipes state at end of test (per testing brief §11)
# =============================================================================

@app.post("/v1/teardown")
async def teardown():
    print(f"[teardown] wiping {len(contexts)} contexts")
    contexts.clear()
    recent_contacts.clear()
    conv_store.reset()
    composer.reset_dedupe()
    # Wipe the on-disk snapshot too
    try:
        if CONTEXTS_FILE.exists():
            CONTEXTS_FILE.unlink()
    except Exception:
        pass
    return {"wiped": True}
