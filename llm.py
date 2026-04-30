"""
LLM provider abstraction.

Primary: Gemini 2.5 Flash (free tier — 500 RPD, 10 RPM).
Fallback: Gemini 2.0 Flash (more headroom — 1500 RPD, 15 RPM) if 2.5 hits limit.
Optional: Groq Llama 3.3 70B if both Gemini fail (uncomment block to enable).

Configure via environment variables:
    GEMINI_API_KEY      — required
    GEMINI_MODEL        — default: gemini-2.5-flash
    GEMINI_FALLBACK     — default: gemini-2.0-flash
    GROQ_API_KEY        — optional, used only if both Gemini calls fail
"""
import os
import json
import asyncio
import httpx
from typing import Optional

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-preview-04-17")
GEMINI_FALLBACK = os.environ.get("GEMINI_FALLBACK", "gemini-1.5-flash")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Single async HTTP client reused across calls (connection pooling)
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    return _client


async def _gemini_call(model: str, system: str, user: str) -> str:
    """Single Gemini call. Raises on non-200 or empty response."""
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.0,           # deterministic per challenge requirement
            "topP": 0.95,
            "maxOutputTokens": 800,
            "responseMimeType": "application/json",  # force JSON output
        },
        "safetySettings": [
            # relax safety filters — merchant-engagement copy occasionally trips
            # health/medical filters (pharmacy, dentist categories)
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in ["HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
                      "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT"]
        ],
    }

    url = GEMINI_URL.format(model=model) + f"?key={GEMINI_KEY}"
    client = _get_client()

    resp = await client.post(url, json=body, headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise RuntimeError(f"Gemini returned empty content: {data}")
    text = parts[0].get("text", "").strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty text: {data}")
    return text


async def _groq_call(system: str, user: str) -> str:
    """Optional Groq fallback — Llama 3.3 70B on free tier."""
    if not GROQ_KEY:
        raise RuntimeError("GROQ_API_KEY not set (and Gemini exhausted)")

    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    client = _get_client()
    resp = await client.post(
        GROQ_URL, json=body,
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def complete(system: str, user: str) -> str:
    """
    Compose with primary model; on rate-limit / 5xx, fall back.
    Returns raw text (expected to be JSON given responseMimeType=application/json).

    Total worst-case latency budget: 3 attempts × ~5s = 15s, well under 30s tick timeout.
    """
    last_error: Optional[Exception] = None

    for attempt_model in [GEMINI_MODEL, GEMINI_FALLBACK]:
        try:
            return await _gemini_call(attempt_model, system, user)
        except httpx.HTTPStatusError as e:
            # 403 = forbidden (bad key), 404 = model not found, 429 = rate-limited, 5xx = transient — try fallback
            if e.response.status_code in (403, 404, 429, 500, 502, 503, 504):
                last_error = e
                await asyncio.sleep(0.5)
                continue
            raise
        except Exception as e:
            last_error = e
            continue

    # Both Gemini variants failed — try Groq if configured
    if GROQ_KEY:
        try:
            return await _groq_call(system, user)
        except Exception as e:
            last_error = e

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


async def shutdown():
    """Close the shared HTTP client on app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
