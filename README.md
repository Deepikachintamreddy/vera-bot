---
title: Vera Bot — magicpin AI Challenge
emoji: 🤖
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Vera Bot — magicpin AI Challenge submission

A 4-context composer for the magicpin Vera challenge. Builds WhatsApp messages
to Indian merchants (and their customers) by routing each trigger through a
family-specific prompt, validating the LLM's output for hallucination and
language match, and re-prompting once on failure.

> **Solo submission.** Challenge submission window closes **2 May 2026, 11:59 PM IST.**
> Bot must remain reachable through **5 May 2026** for adaptive-injection scoring.

## What it does (the 5 endpoints)

| Endpoint | Purpose |
| --- | --- |
| `GET  /v1/healthz` | Liveness + how many contexts are loaded |
| `GET  /v1/metadata` | Team identity + approach for the judge |
| `POST /v1/context` | Idempotent push of category / merchant / customer / trigger contexts (disk-persisted) |
| `POST /v1/tick` | Periodic wake-up — bot picks active triggers + composes messages |
| `POST /v1/reply` | Handles inbound reply from simulated merchant/customer |
| `POST /v1/teardown` | Optional, wipes state at end of test |

## Architecture

```
  Judge harness ─push contexts──► /v1/context ──► in-memory + disk-backed store
                 ─tick──────────► /v1/tick    ──► composer.compose_many ─┐
                                                          │              │
                                                  decision-quality triage
                                                  (urgency desc, dedupe by
                                                  merchant, recent-contact
                                                  demotion, cap at 5)
                                                          ▼
                                                  per-family prompt
                                                          ▼
                                                  LLM (Gemini 2.5 Flash)
                                                          ▼
                                                  validator.validate
                                                          ▼ (re-prompt once on fail)
                                                  action JSON ◄──────────┘
                                                          │
                                                          ▼
                                                  recorded in conv_store

                 ─reply─────────► /v1/reply   ──► state detectors
                                                  (auto-reply / intent / hostile)
                                                          ▼
                                                  composer.reply_for ──► LLM ──► action
```

Six modules, ~900 lines of Python:
- **`app.py`** — FastAPI server + 5 endpoints + in-memory + disk-backed stores
- **`composer.py`** — orchestrates LLM calls, decision-quality triage, parallel composition, dedupe
- **`prompts.py`** — 8 trigger-family prompts with case-study few-shots, official rubric vocabulary
- **`validator.py`** — hallucination + language + CTA + taboo checker (the score multiplier)
- **`state.py`** — conversation store + auto-reply / intent / hostile detection
- **`llm.py`** — Gemini 2.5 Flash primary + 2.0 Flash fallback + optional Groq

## How this maps to the official 5-dimension rubric

| Rubric dimension | What this bot does |
| --- | --- |
| **Decision quality** | `compose_many` does explicit triage *before* calling the LLM: urgency desc sort, internal-before-external, per-merchant dedupe (no double-messaging), drop urgency-1 if higher exists, demote merchants contacted in the last 4 hours, cap at 5 messages per tick. Restraint scores higher than spam. |
| **Specificity** | Universal-rules prompt explicitly demands a verifiable number/date/source from the contexts. Validator flags fabricated numbers and re-prompts. The four official engagement levers (proof / urgency / curiosity / simple yes/no) are named in the system prompt. |
| **Category fit** | System prompt is rebuilt per trigger kind from the category's voice rules, taboos, and salutation examples. Vocab taboos are validator-checked. |
| **Merchant fit** | Compact merchant payload includes name, owner_first_name, signals, customer_aggregate, active offers, performance.delta_7d. |
| **Engagement compulsion** | Each family prompt names the specific lever to use; the system prompt also hard-codes the "190 people in your locality are searching for X" gold-standard pattern so the LLM has a reference. |

## Replay-test handling (top-10 only, per challenge phase 4)

| Scenario | Detection | Action |
| --- | --- | --- |
| **Auto-reply hell** | `state.is_auto_reply` matches WA-Business canned phrases ("Thank you for contacting...", "team tak pahuncha...", "automated assistant") + 3rd verbatim repetition | Send one human-targeted nudge on first detection; `end` on second detection |
| **Intent transition** | `state.is_affirmative_intent` matches "yes", "ok", "lets do it", "go ahead", "haan", "judna hai" etc. | Reply prompt is re-injected with explicit ACTION-mode instruction, forbidding further qualifying questions |
| **Hostile / off-topic** | `state.is_hostile` matches "stop messaging", "spam", "useless", "band karo" | Hard-coded polite exit; no LLM call needed |

## Local run

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=AIza...                     # from aistudio.google.com
uvicorn app:app --host 0.0.0.0 --port 7860 --reload
```

In another terminal, copy the challenge `dataset/` folder next to `test_local.py`
and run the smoke test:

```bash
python test_local.py
```

Expected output: green `PASS` on healthz, metadata, all context pushes,
idempotency, tick (with real composed messages), auto-reply detection, intent
transition, and hostile handling.

For real scoring, run the official judge:

```bash
# Edit judge_simulator.py: BOT_URL=http://localhost:7860 + your scoring LLM key
python judge_simulator.py
```

## Deployment — why HuggingFace Spaces

I considered three free options. Quick comparison (verified Apr 2026):

| Provider | Free? | Sleep behavior | Verdict |
| --- | --- | --- | --- |
| Railway | **No** — $5 trial credit only, then $5/mo subscription required | Always-on (paid) | Skip — not free anymore |
| Render | Yes, but… | Sleeps after **15 min** of inactivity, cold start 30-180s | Risky — judge polls /healthz every 60s during the test (keeps it warm), but if asleep when judge first hits, 3 consecutive healthz fails = disqualified |
| **HuggingFace Spaces (Docker)** | **Yes** | Sleeps only after **48 hours** of no traffic | **Chosen** — judge keeps it awake the whole window. Generous CPU (2 vCPU + 16 GB RAM). |

**HF Spaces caveat:** free Spaces can occasionally restart (no persistent disk
guarantee). Mitigation: this bot persists the context store to `/tmp/vera-state/`
on every push. If a worker restarts mid-test, contexts are rebuilt on startup.

### Deploy steps

1. Create a Space at <https://huggingface.co/new-space>
   - SDK: **Docker**
   - Visibility: Public
   - Hardware: CPU basic (free)
2. Push these files (git push):
   ```
   app.py composer.py prompts.py validator.py state.py llm.py
   Dockerfile requirements.txt README.md
   ```
3. Settings → **Variables and secrets** → add **Secret**:
   - `GEMINI_API_KEY` = your key from <https://aistudio.google.com/apikey>
4. Optional secrets:
   - `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`
   - `GEMINI_MODEL` (default `gemini-2.5-flash`)
   - `GEMINI_FALLBACK` (default `gemini-2.0-flash` — higher RPD)
   - `GROQ_API_KEY` (optional emergency fallback)
5. Wait for build (~3-5 min). Public URL: `https://YOUR_USERNAME-SPACE_NAME.hf.space`
6. Verify:
   ```bash
   curl https://YOUR_USERNAME-SPACE_NAME.hf.space/v1/healthz
   ```
   Should return `{"status": "ok", ...}`.
7. Submit that URL on the magicpin portal. **Wake the Space** by hitting `/v1/healthz`
   right before the judge starts so there's no cold-start risk.
8. Keep the Space alive through 5 May 2026 — the judge runs adaptive scoring across
   the whole 3-day window.

### HF Spaces quirks to know

- Spaces expose port **7860** by default; the Dockerfile already binds to it.
- `--workers 1` in the Dockerfile is required — multi-worker uvicorn shards the
  in-memory context store across processes. Single worker keeps everything
  consistent.
- Don't run multiple deploys in quick succession; HF queues builds and the
  previous Space stays "running" until the new one finishes.

## Knobs

All env vars are optional except `GEMINI_API_KEY`:

| Var | Default | Notes |
| --- | --- | --- |
| `GEMINI_API_KEY` | _required_ | aistudio.google.com — free tier (500 RPD on 2.5 Flash) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | swap to `gemini-2.0-flash` if you hit RPD limits |
| `GEMINI_FALLBACK` | `gemini-2.0-flash` | tried on 429/5xx from primary |
| `GROQ_API_KEY` | _unset_ | optional last-resort fallback to Llama 3.3 70B |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | |
| `TEAM_NAME` | `GrowthGenie` | shown in `/v1/metadata` |
| `TEAM_MEMBERS` | `Deepika Chintamreddy` | comma-separated for multiple (challenge is solo, but leaving multi support) |
| `CONTACT_EMAIL` | `deepika@example.com` | |
| `VERA_PERSIST_DIR` | `/tmp/vera-state` | where contexts.json is written |

## Tradeoffs made

- **Single uvicorn worker** — required for the in-memory store; trades off
  throughput we don't need (judge sends ≤10 RPS).
- **No retrieval / no embeddings** — the digest is small enough to inline. With
  more digest items (50+), I'd switch to a per-category embedding lookup.
- **In-memory + disk persist, no Redis** — judge tests are 60 min with ~255
  contexts; in-memory is fine and disk-snapshot is restart insurance.
- **Validator is best-effort** — false positives on hallucination check are OK
  (one re-prompt cost); false negatives mean a bad message ships.
- **Per-trigger conv_id** — keeps multi-turn replays simple at the cost of not
  consolidating multiple triggers into one merchant conversation.

## What additional context would have helped most

1. **Sample multi-turn replays from production Vera** — the brief shows 3
   patterns; 20 would let me tune the reply prompt much harder.
2. **Confusion matrix on auto-reply detection** — what false-positive rate
   does production-Vera tolerate? I tuned for ~3% but flying blind.
3. **Per-merchant conversation budget** — is 5 messages/24h fine, or is the
   ceiling 2? Affects how aggressive `tick` should be about acting on every
   available trigger vs. saving them.

## Files

```
.
├── app.py              FastAPI server, 5 endpoints, disk-backed context store
├── composer.py         Decision-quality triage, parallel composition, reply routing
├── prompts.py          Universal rules + 8 family prompts + few-shots + 4 official levers
├── validator.py        Hallucination + language + CTA + taboo validator
├── state.py            Conversation store + 3 detectors (auto/intent/hostile)
├── llm.py              Gemini-primary + Groq-fallback client
├── Dockerfile          HF Spaces compatible (port 7860, single worker)
├── requirements.txt    fastapi / uvicorn / httpx / pydantic
├── test_local.py       Smoke test — exercises all 5 endpoints
└── README.md           This file (also serves as HF Spaces card)
```

