"""
Conversation state machine.

Tracks per-conversation history. Detects:
  - Auto-replies (same text 3+ times OR matches WA-Business canned patterns)
  - Intent transitions (merchant says "yes" / "lets do it" / "go ahead")
  - Hostile messages (stop / spam / useless / abuse)
  - 3-strikes silence (no merchant reply in 3 outbound nudges)

These detections drive reply routing on /v1/reply:
  - Auto-reply → return {"action": "end"} after one polite acknowledgment
  - Intent transition → ACTION mode (no more qualifying questions)
  - Hostile → return {"action": "end"} with apology body
  - Otherwise → normal LLM-composed reply
"""
import re
import time
from dataclasses import dataclass, field
from typing import Optional


# --- Regexes for pattern detection ---

WA_AUTO_REPLY_PHRASES = [
    "thank you for contacting",
    "thanks for contacting",
    "we will get back",
    "will respond shortly",
    "team will respond",
    "team tak pahuncha",        # Hindi: "I'll forward to team" (production-Vera example)
    "automated assistant",
    "automated reply",
    "auto-reply",
    "this is an automated",
    "out of office",
    "currently unavailable",
    "sujhaav hamari team",       # Hindi: "suggestions to our team"
    "jaankari ke liye bahut",    # Hindi: "thank you for the information"
]

INTENT_AFFIRMATIVE_PHRASES = [
    "yes lets do it", "lets do it", "let's do it", "let me know",
    "yes please", "yes, please", "go ahead", "please proceed",
    "ok proceed", "okay proceed", "sounds good", "ok do it",
    "i want to", "i would like to", "main chahta hoon", "mujhe chahiye",
    "mujhe karna hai", "hum karenge", "main karunga",
    "sure", "ok", "okay", "haan", "ji haan", "haanji",
    "yes", "yep", "yeah", "yup", "absolutely", "definitely",
    "bilkul", "zaroor", "kar do", "kar dijiye",
    "please share", "share kar do", "send it", "bhej do",
    "draft kar do", "draft it", "draft karo",
    "i am interested", "interested", "join karna hai", "judna hai",
    "join karega", "judrna hai",
]

HOSTILE_PHRASES = [
    "stop messaging", "stop spamming", "stop sending", "stop calling",
    "do not message", "don't message", "dont message",
    "useless", "spam", "harassment", "bothering me",
    "f off", "fuck off", "shut up", "leave me alone",
    "unsubscribe", "remove me", "block me",
    "band karo", "rok do", "mat bhejo",
    "kya bakwas", "bakwas hai",
]

NOT_INTERESTED_PHRASES = [
    "not interested", "no thanks", "no thank you", "not now",
    "maybe later", "later", "another time", "no need",
    "nahi chahiye", "abhi nahi", "interested nahi hoon",
    "no",
]


@dataclass
class TurnRecord:
    role: str            # "vera" | "merchant" | "customer"
    body: str
    ts: float = field(default_factory=time.time)


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    turns: list[TurnRecord] = field(default_factory=list)
    ended: bool = False
    ended_reason: Optional[str] = None
    consecutive_no_reply: int = 0


def _norm(text: str) -> str:
    """Normalize for comparison: lowercase, strip punctuation, collapse whitespace."""
    t = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


def is_auto_reply(message: str, prior_inbound: list[str]) -> bool:
    """
    True if the message is likely an auto-reply.
    Two heuristics:
      1. Matches a known WA-Business auto-reply phrase (case-insensitive).
      2. Same normalized text appeared 2+ times before (3rd+ occurrence).
    """
    msg_norm = _norm(message)
    if not msg_norm:
        return False
    # Heuristic 1: canned phrase match
    for phrase in WA_AUTO_REPLY_PHRASES:
        if phrase in msg_norm:
            return True
    # Heuristic 2: repetition (3rd verbatim occurrence)
    same_count = sum(1 for prior in prior_inbound if _norm(prior) == msg_norm)
    if same_count >= 2:  # this is the 3rd time we've seen it
        return True
    return False


def is_affirmative_intent(message: str) -> bool:
    """True if merchant gave clear affirmative commitment (intent transition signal)."""
    msg_norm = _norm(message)
    if not msg_norm:
        return False
    # Short pure-affirmatives ("yes", "ok", "haan") count
    if msg_norm in {"yes", "ok", "okay", "haan", "ji haan", "sure", "yep",
                    "yeah", "bilkul", "zaroor"}:
        return True
    # Phrase containment for longer messages
    return any(p in msg_norm for p in INTENT_AFFIRMATIVE_PHRASES)


def is_hostile(message: str) -> bool:
    """True if merchant message is hostile/abusive/blocked-style."""
    msg_norm = _norm(message)
    if not msg_norm:
        return False
    return any(p in msg_norm for p in HOSTILE_PHRASES)


def is_not_interested(message: str) -> bool:
    """Soft-rejection — merchant declined politely. Distinct from hostile."""
    msg_norm = _norm(message)
    if not msg_norm:
        return False
    if msg_norm in {"no", "nope", "na", "nahi"}:
        return True
    return any(p in msg_norm for p in NOT_INTERESTED_PHRASES)


# --- In-memory conversation store ---

class ConversationStore:
    """Thread-unsafe in-memory store. FastAPI endpoints are async-serialized
    per process, so this is safe for the single-worker uvicorn we deploy."""

    def __init__(self):
        self._convos: dict[str, Conversation] = {}

    def get(self, conv_id: str) -> Conversation:
        if conv_id not in self._convos:
            self._convos[conv_id] = Conversation(conversation_id=conv_id)
        return self._convos[conv_id]

    def record_outbound(self, conv_id: str, body: str,
                        merchant_id: Optional[str] = None,
                        customer_id: Optional[str] = None):
        c = self.get(conv_id)
        if merchant_id and not c.merchant_id:
            c.merchant_id = merchant_id
        if customer_id and not c.customer_id:
            c.customer_id = customer_id
        c.turns.append(TurnRecord(role="vera", body=body))
        c.consecutive_no_reply += 1

    def record_inbound(self, conv_id: str, body: str,
                       from_role: str = "merchant") -> Conversation:
        c = self.get(conv_id)
        c.turns.append(TurnRecord(role=from_role, body=body))
        c.consecutive_no_reply = 0
        return c

    def prior_inbound_bodies(self, conv_id: str) -> list[str]:
        c = self.get(conv_id)
        return [t.body for t in c.turns if t.role in ("merchant", "customer")]

    def vera_outbound_count(self, conv_id: str) -> int:
        c = self.get(conv_id)
        return sum(1 for t in c.turns if t.role == "vera")

    def end(self, conv_id: str, reason: str):
        c = self.get(conv_id)
        c.ended = True
        c.ended_reason = reason

    def is_ended(self, conv_id: str) -> bool:
        return conv_id in self._convos and self._convos[conv_id].ended

    def history_for_prompt(self, conv_id: str, n: int = 6) -> list[dict]:
        c = self.get(conv_id)
        return [{"role": t.role, "body": t.body} for t in c.turns[-n:]]

    def reset(self):
        """Wipe state — used by /v1/teardown (optional endpoint)."""
        self._convos.clear()
