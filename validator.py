"""
Post-LLM validator.

The single biggest score-leverage piece nobody else will build. Catches:
  - Fabricated numbers (numbers in body that don't appear anywhere in contexts)
  - Taboo vocabulary leakage (category-defined banned terms)
  - Language mismatch (English-only body when merchant prefers hi-en mix)
  - Missing CTA (no question or imperative in last 2 sentences)
  - Multi-choice CTA outside booking flows (penalized by judge)

Returns (is_valid: bool, problems: list[str]). The composer re-prompts ONCE
on validation failure with the problems appended to the prompt.
"""
import re
from typing import Optional

# Hindi unicode range — used for language-mix detection
HINDI_RE = re.compile(r"[\u0900-\u097F]")
# Common Hindi words written in Latin script (Hinglish)
# CRITICAL: Exclude overly-generic English words that create false positives (e.g., "me", "to")
HINGLISH_TOKENS = {
    "aap", "apke", "apki", "apka", "aapko", "aapki", "aapka", "aapke",
    "hai", "hain", "ho", "ka", "ki", "ke", "ko", "se", "mein",  # removed "me"
    "kya", "kyu", "kyun", "kyon", "kab", "kahan", "kitna", "kitni",
    "yahan", "wahan", "abhi", "phir", "lekin", "agar", "toh",  # removed "to"
    "haan", "nahi", "chahiye", "chahta", "chahti", "chalega", "chaliye",
    "namaste", "ji", "shukriya", "dhanyavaad", "bhaiya", "didi",
    "kar", "karo", "karna", "karni", "kiya", "kiye", "rakho", "rakhi",
    "wala", "wali", "waale", "ek", "do", "teen", "char", "paanch",
    "rahe", "rahi", "raha", "tha", "thi", "the", "hoga", "hogi",
    "achha", "accha", "acha", "samajh", "samjha", "samjhi",
    "ya", "aur", "yaa", "liye", "ye", "wo",  # Added more specific Hindi words
}

# Pure-info trigger kinds where a CTA is acceptable but not required
INFO_ONLY_KINDS = {"research_digest", "regulation_change", "supply_alert"}

# Trigger kinds where multi-choice slot CTAs ARE acceptable (booking flows)
BOOKING_KINDS = {"recall_due", "appointment_tomorrow", "chronic_refill_due",
                 "wedding_package_followup", "bridal_followup"}


def _all_numbers(text: str) -> set[str]:
    """Extract every number-looking token from text — integers, decimals, percentages."""
    # match: 12, 12.3, 12,345, 12%, ₹299, 38%, 6mo, 3-month
    nums = set()
    for m in re.finditer(r"\d[\d,\.]*", text):
        token = m.group().rstrip(",.")
        # normalize "12,345" → "12345" for comparison
        nums.add(token.replace(",", ""))
        nums.add(token)
    return nums


def _flatten_contexts_text(category: dict, merchant: dict, trigger: dict,
                           customer: Optional[dict]) -> str:
    """Concatenate every string and number value from all contexts for hallucination checks."""
    import json
    parts = [json.dumps(category, ensure_ascii=False),
             json.dumps(merchant, ensure_ascii=False),
             json.dumps(trigger, ensure_ascii=False)]
    if customer:
        parts.append(json.dumps(customer, ensure_ascii=False))
    return " ".join(parts)


def _detect_language_mix(text: str) -> bool:
    """True if the message uses Hindi (devanagari) or Hinglish tokens (at least 3 tokens to avoid false positives)."""
    if HINDI_RE.search(text):
        return True
    tokens = re.findall(r"\b[a-z]+\b", text.lower())
    hits = sum(1 for t in tokens if t in HINGLISH_TOKENS)
    return hits >= 3  # Increased from 2 to 3 to avoid false positives


def _has_cta(body: str) -> bool:
    """True if the last ~2 sentences contain a CTA marker."""
    # Take last 200 chars (roughly last 1-2 sentences)
    tail = body[-220:].lower()
    cta_markers = [
        "?", "reply ", "want me", "want to", "tell us",
        "shall i", "should i", "let me know",
        "confirm", "go ahead", "say yes",
    ]
    return any(m in tail for m in cta_markers)


def _has_multi_choice_cta(body: str) -> bool:
    """True if body has 'Reply 1 for X, 2 for Y, 3 for Z' style multi-choice."""
    # Look for "reply 1 ... 2 ... 3" or "press 1 ... 2 ..." patterns
    body_l = body.lower()
    if re.search(r"reply\s+1.*\s+2.*\s+3", body_l):
        return True
    if re.search(r"\b1\b.*\b2\b.*\b3\b", body_l) and "reply" in body_l:
        return True
    return False


def validate(body: str, cta_label: str, category: dict, merchant: dict,
             trigger: dict, customer: Optional[dict]) -> tuple[bool, list[str]]:
    """
    Returns (is_valid, list_of_problems_for_re-prompt).

    Validation is best-effort — false positives are okay (we re-prompt once and
    move on). False negatives are the real risk and we accept some.
    """
    problems: list[str] = []

    # --- Empty / too-short body ---
    if not body or len(body.strip()) < 30:
        return False, ["Body is empty or too short. Compose a real message of at least 30 chars."]

    # --- Language match ---
    languages = (merchant.get("identity", {}) or {}).get("languages", [])
    if customer:
        cust_pref = (customer.get("identity", {}) or {}).get("language_pref", "")
        if "hi" in cust_pref or "mix" in cust_pref:
            languages = languages + ["hi"]
    wants_mix = "hi" in languages
    has_mix = _detect_language_mix(body)
    if wants_mix and not has_mix:
        problems.append(
            "The merchant/customer language preference includes Hindi but the body is "
            "pure English. Add natural Hindi-English code-mix (e.g., 'aapke liye', "
            "'2 slots ready hain', 'apke', 'kya', 'haan')."
        )

    # --- Taboo vocabulary ---
    taboos = (category.get("voice", {}) or {}).get("vocab_taboo", []) or []
    body_l = body.lower()
    leaked = [t for t in taboos if isinstance(t, str) and t.lower() in body_l]
    if leaked:
        problems.append(
            f"Body uses banned vocabulary for this category: {leaked}. "
            f"Rewrite without these terms."
        )

    # --- CTA presence (skip for pure-info triggers if cta=='none') ---
    kind = trigger.get("kind", "")
    if cta_label != "none" and not _has_cta(body):
        problems.append(
            "No clear CTA detected in the last 2 sentences. End with a question or "
            "single ask (e.g., 'Want me to draft X?', 'Reply YES', 'Confirm').")

    # --- Multi-choice CTA outside booking flows ---
    if _has_multi_choice_cta(body) and kind not in BOOKING_KINDS:
        problems.append(
            "Body uses multi-choice CTA (Reply 1 for X, 2 for Y...) which is only "
            "allowed for booking flows. Use a single binary or open-ended ask.")

    # --- Hallucinated numbers (most important check) ---
    body_nums = _all_numbers(body)
    ctx_blob = _flatten_contexts_text(category, merchant, trigger, customer)
    ctx_nums = _all_numbers(ctx_blob)
    # Allow common temporal numbers and small integers that everybody writes
    SAFE_NUMBERS = {"1", "2", "3", "4", "5", "10", "30", "60", "100", "0",
                    "2026", "2025", "2024", "12", "24", "48", "7", "15", "20",
                    "1st", "2nd", "3rd", "4th", "5th"}
    fabricated = [n for n in body_nums
                  if n not in ctx_nums and n not in SAFE_NUMBERS
                  and len(n) >= 2  # ignore single-digit incidentals
                  and n.replace(".", "").isdigit()]
    # be conservative — only flag if 2+ fabricated numbers (1 false positive happens)
    if len(fabricated) >= 2:
        problems.append(
            f"Body contains numbers not present in any context: {fabricated[:5]}. "
            f"Either remove them or replace with numbers actually present in the contexts. "
            f"DO NOT invent statistics, prices, percentages, or counts.")

    return (len(problems) == 0, problems)
