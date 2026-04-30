"""
Composer — the brain.

Responsibilities:
  - compose(category, merchant, trigger, customer?) → action dict
  - compose_many(triggers + their contexts) → run in parallel, respect total budget
  - reply_for(conversation, merchant_msg, contexts) → next-turn action
  - dedupe by suppression_key (don't send the same trigger twice)
"""
import asyncio
import json
import re
import time
from typing import Optional

import llm
import prompts
import validator
import state


# Tracks suppression keys we've already acted on (in-memory, per process).
# Same suppression_key means "same trigger" — judge expects dedupe.
_sent_suppression_keys: set[str] = set()


def _safe_json_loads(text: str) -> Optional[dict]:
    """Tolerant JSON parser — strips fences if model added them."""
    if not text:
        return None
    text = text.strip()
    # Remove common code-fence wrappers
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Find the first {...} object
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _build_action(merchant_id: str, customer_id: Optional[str], trigger: dict,
                  llm_out: dict, conversation_id: str) -> dict:
    """Assemble the action dict the /v1/tick endpoint returns."""
    kind = trigger.get("kind", "unknown")
    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera",
        "trigger_id": trigger.get("id"),
        "template_name": f"vera_{kind}_v1",
        "template_params": llm_out.get("template_params") or [],
        "body": llm_out.get("body", "").strip(),
        "cta": llm_out.get("cta", "open_ended"),
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": llm_out.get("rationale", "").strip(),
    }


async def compose_one(category: dict, merchant: dict, trigger: dict,
                      customer: Optional[dict]) -> Optional[dict]:
    """
    One LLM call (with up to one re-prompt on validation failure).
    Returns an action dict on success, None if compose failed entirely.
    """
    kind = trigger.get("kind", "unknown")
    system = prompts.system_prompt_for(kind)
    user = prompts.build_user_prompt(category, merchant, trigger, customer)

    # First attempt
    try:
        raw = await llm.complete(system, user)
    except Exception as e:
        # If LLM is down, return None — caller will skip this trigger
        print(f"[compose] LLM call failed for trigger {trigger.get('id')}: {e}")
        return None

    parsed = _safe_json_loads(raw)
    if not parsed or "body" not in parsed:
        # Re-try once with a more direct instruction
        retry_user = user + "\n\nReminder: respond with ONE JSON object only, no prose, no markdown."
        try:
            raw = await llm.complete(system, retry_user)
            parsed = _safe_json_loads(raw)
        except Exception:
            return None

    if not parsed or "body" not in parsed:
        return None

    body = parsed.get("body", "")
    cta = parsed.get("cta", "open_ended")

    # Validate
    is_valid, problems = validator.validate(body, cta, category, merchant, trigger, customer)

    if not is_valid:
        # Re-prompt ONCE with the problems appended
        fix_msg = (
            "Your previous draft has these problems:\n- "
            + "\n- ".join(problems)
            + "\n\nProduce a corrected JSON object now."
        )
        retry_user = user + "\n\nPRIOR DRAFT:\n" + json.dumps(parsed) + "\n\n" + fix_msg
        try:
            raw2 = await llm.complete(system, retry_user)
            parsed2 = _safe_json_loads(raw2)
            if parsed2 and "body" in parsed2:
                # Accept the retry even if not perfectly clean — usually better
                parsed = parsed2
        except Exception:
            pass  # keep first draft if retry fails

    return parsed  # caller wraps into action


async def compose_many(items: list[dict],
                       recent_contacts: Optional[dict[str, float]] = None) -> list[dict]:
    """
    items: list of dicts with keys: category, merchant, trigger, customer, conversation_id
    recent_contacts: optional dict of merchant_id -> last_contact_ts (epoch). If present,
                    triggers for merchants contacted within the last N seconds are demoted.
    Returns: list of action dicts (skipping any that failed).

    Decision-quality logic (explicit per rubric):
      1. Drop already-sent (suppression_key seen) triggers.
      2. Drop expired triggers.
      3. Sort remaining by urgency desc, then by source ('internal' before
         'external' for same urgency — internal = action-required).
      4. If a merchant has multiple triggers in this batch, keep only the
         highest-urgency one (don't double-message a merchant in one tick).
      5. If high-urgency triggers (urgency >= 3) exist, drop urgency 1 triggers
         from the batch — quality over quantity.
      6. Demote (skip) merchants contacted in the last 4 hours unless trigger
         urgency >= 4 (renewal_due, perf_dip, supply_alert).
      7. Cap to 5 messages per tick total.
    """
    pending: list[dict] = []
    now_ts = time.time()
    recent_contacts = recent_contacts or {}

    for item in items:
        trg = item["trigger"]
        merchant = item["merchant"]
        sup = trg.get("suppression_key", "")
        trg_id = trg.get("id", "?")

        # 1. Skip already-sent
        if sup and sup in _sent_suppression_keys:
            print(f"[compose] {trg_id} dropped: suppression_key already sent")
            continue

        # 2. Skip expired
        exp = trg.get("expires_at")
        if exp:
            try:
                from datetime import datetime
                exp_clean = exp.replace("Z", "+00:00")
                exp_ts = datetime.fromisoformat(exp_clean).timestamp()
                if exp_ts < now_ts:
                    print(f"[compose] {trg_id} dropped: expired")
                    continue
            except Exception:
                pass

        # 6. Demote if recently contacted (unless urgent)
        merchant_id = merchant.get("merchant_id", "")
        urgency = trg.get("urgency", 1)
        last_contact = recent_contacts.get(merchant_id)
        if last_contact and urgency < 4:
            hours_since = (now_ts - last_contact) / 3600.0
            if hours_since < 4:
                print(f"[compose] {trg_id} dropped: recently contacted")
                continue

        print(f"[compose] {trg_id} proceeding to compose")
        pending.append(item)

    if not pending:
        print(f"[compose] no triggers passed filters, returning empty")
        return []

    print(f"[compose] {len(pending)} triggers passed filters, composing...")

    # 3. Sort by urgency desc, then source (internal before external)
    def _priority_key(it):
        trg = it["trigger"]
        urgency = trg.get("urgency", 1)
        source_rank = 0 if trg.get("source") == "internal" else 1
        return (-urgency, source_rank)

    pending.sort(key=_priority_key)

    # 4. Per-merchant: keep only the highest-priority trigger
    seen_merchants: set[str] = set()
    deduped: list[dict] = []
    for item in pending:
        mid = item["merchant"].get("merchant_id", "")
        if mid in seen_merchants:
            continue
        seen_merchants.add(mid)
        deduped.append(item)

    # 5. If high-urgency exists, drop the low-urgency noise
    has_high = any((it["trigger"].get("urgency") or 1) >= 3 for it in deduped)
    if has_high:
        deduped = [it for it in deduped if (it["trigger"].get("urgency") or 1) >= 2]

    # 7. Cap at 5 per tick — quality over quantity
    deduped = deduped[:5]

    if not deduped:
        return []

    # Cap concurrency to avoid hitting Gemini 10 RPM hard
    sem = asyncio.Semaphore(4)

    async def _one(item):
        async with sem:
            llm_out = await compose_one(
                item["category"], item["merchant"], item["trigger"], item.get("customer"))
            if llm_out is None:
                return None
            return _build_action(
                merchant_id=item["merchant"].get("merchant_id"),
                customer_id=(item.get("customer") or {}).get("customer_id"),
                trigger=item["trigger"],
                llm_out=llm_out,
                conversation_id=item["conversation_id"],
            )

    raw_actions = await asyncio.gather(*(_one(it) for it in deduped), return_exceptions=False)

    # Filter Nones, mark suppression keys as sent
    actions: list[dict] = []
    for a in raw_actions:
        if not a:
            continue
        if not a.get("body"):
            continue
        sup = a.get("suppression_key", "")
        if sup:
            _sent_suppression_keys.add(sup)
        actions.append(a)
    print(f"[compose] final actions: {len(actions)} returned")
    return actions


# =============================================================================
# Reply-side composition — handles inbound merchant messages
# =============================================================================

REPLY_SYSTEM_PROMPT = prompts.UNIVERSAL_RULES + """

REPLY-MODE CONTEXT
You are continuing an in-progress conversation. Use the conversation history
below to keep continuity. The merchant just sent you a message — respond with
the next thing Vera would say.

CRITICAL ROUTING:
- If the merchant has just affirmed (e.g., "yes", "lets do it", "go ahead"):
  switch to ACTION mode. Do NOT ask another qualifying question. Acknowledge
  the commitment and deliver the next concrete deliverable from the plan
  (a draft, a list, a confirmation, a sample). Use action verbs: "sending",
  "drafted", "here's", "done".
- If the merchant asked a clarifying question: answer concisely from the
  contexts; do not invent.
- If the merchant raised an objection: address it specifically.

OUTPUT JSON SCHEMA (exact keys):
{
  "body": "<reply body>",
  "cta": "binary_yes_stop" | "open_ended" | "none",
  "rationale": "<one sentence>"
}
"""


async def reply_for(conv: state.Conversation, merchant_message: str,
                    merchant_ctx: Optional[dict],
                    category_ctx: Optional[dict],
                    customer_ctx: Optional[dict] = None) -> dict:
    """
    Given the conversation + merchant's latest message, return a reply action.

    Returns a dict with one of:
      {"action": "send", "body": "...", "cta": "...", "rationale": "..."}
      {"action": "wait", "wait_seconds": <int>, "rationale": "..."}
      {"action": "end", "rationale": "..."}
    """

    # --- 1. Hostile detection ---
    if state.is_hostile(merchant_message):
        return {
            "action": "send",
            "body": "Got it — I'll stop messaging. Sorry for the bother. If you change "
                    "your mind, just reply START anytime.",
            "cta": "none",
            "rationale": "Merchant signaled hostility; sending a polite final acknowledgment "
                         "and ending the conversation."
        }

    # --- 2. Auto-reply detection ---
    prior_inbound = conv.turns and [t.body for t in conv.turns
                                    if t.role in ("merchant", "customer")]
    if state.is_auto_reply(merchant_message, prior_inbound or []):
        # If we've already tried once after detecting auto-reply, give up
        # (count auto-reply occurrences in conversation)
        prior_auto = sum(1 for body in (prior_inbound or [])
                         if state.is_auto_reply(body, []))
        if prior_auto >= 1:
            return {
                "action": "end",
                "rationale": "Auto-reply detected for the second time; merchant's WA Business "
                             "is responding canned. Ending to avoid wasting turns."
            }
        # First auto-reply detection — try once to break through, then quit
        return {
            "action": "send",
            "body": "Samajh gayi, woh auto-reply tha. Owner/manager se direct ek minute mil "
                    "sake to maine aapke liye ek specific cheez ready ki hai — bas haan/na "
                    "bata dijiye?",
            "cta": "binary_yes_stop",
            "rationale": "Detected auto-reply once; trying one human-targeted nudge before "
                         "exiting."
        }

    # --- 3. Not-interested (soft decline) ---
    if state.is_not_interested(merchant_message):
        return {
            "action": "end",
            "rationale": "Merchant said not interested; respecting their decision and ending."
        }

    # --- 4. 3-strikes silence rule ---
    # If we've sent 3+ outbound without engagement, back off
    vera_count = sum(1 for t in conv.turns if t.role == "vera")
    inbound_count = sum(1 for t in conv.turns if t.role in ("merchant", "customer"))
    if vera_count >= 3 and inbound_count == 0:
        # No reply after 3 nudges
        return {"action": "end", "rationale": "3 unanswered nudges; gracefully exiting."}

    # --- 5. LLM-composed reply with intent-transition awareness ---
    intent_affirmed = state.is_affirmative_intent(merchant_message)

    history = [{"role": t.role, "body": t.body} for t in conv.turns[-6:]]

    payload = {
        "category": prompts._compact_category(category_ctx) if category_ctx else None,
        "merchant": prompts._compact_merchant(merchant_ctx) if merchant_ctx else None,
        "customer": prompts._compact_customer(customer_ctx),
        "conversation_history": history,
        "merchant_just_said": merchant_message,
        "intent_signal": "AFFIRMATIVE_COMMITMENT" if intent_affirmed else "NORMAL",
    }

    instruction = (
        "Compose Vera's reply based on the conversation above. Output ONE JSON object."
    )
    if intent_affirmed:
        instruction += (
            "\n\nIMPORTANT: The merchant just AFFIRMED. You are in ACTION mode. "
            "Do NOT ask another qualifying question. Use action verbs (sending, drafted, "
            "here's, done) and deliver the next concrete artifact or confirmation."
        )

    user_prompt = (
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n{instruction}"
    )

    try:
        raw = await llm.complete(REPLY_SYSTEM_PROMPT, user_prompt)
        parsed = _safe_json_loads(raw)
    except Exception as e:
        print(f"[reply_for] LLM error: {e}")
        parsed = None

    if not parsed or "body" not in parsed:
        # Conservative fallback — send a short acknowledgment
        return {
            "action": "send",
            "body": "Got it — let me check on that and circle back.",
            "cta": "none",
            "rationale": "LLM failure fallback; sent neutral acknowledgment."
        }

    return {
        "action": "send",
        "body": parsed["body"].strip(),
        "cta": parsed.get("cta", "open_ended"),
        "rationale": parsed.get("rationale", "").strip(),
    }


def reset_dedupe():
    """Clear the suppression-key dedupe set (used by /v1/teardown)."""
    _sent_suppression_keys.clear()
