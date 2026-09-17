# OpenLIT agent demo

A 1-file OpenAI Agents SDK support desk, instrumented with OpenLIT. Built so you can:

1. Scan it with Trustabl and show findings
2. Fix the static issues, push, scan again
3. Run the agent, open traces, run AI Analysis
4. Tighten the loop, run again, show cleaner traces

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add OPENAI_API_KEY
```

Push this repo to GitHub. Trustabl only scans `https://github.com/owner/repo`.

Point `.env` at your OpenLIT OTLP endpoint (`http://127.0.0.1:4318` for a local collector).

---

## 1. First scan (expect findings)

In OpenLIT: **Scanner** (`/scanner`) → add a **Trustabl** connector → paste the GitHub URL → **Run scan**.

This `agent.py` is written to trip common OpenAI Agents SDK rules, including:

| Rule | Why it fires |
| --- | --- |
| OAI-001 / OAI-002 / OAI-007 | Tools have no docstring, no types, names like `run` / `process` |
| OAI-012 / OAI-101 / OAI-104 | `run` spawns a subprocess; agent has no input guardrails |
| OAI-006 | `process` opens a model-supplied path with no `.resolve()` |
| OAI-009 | `create_ticket` mutates state with no `idempotency_key` |
| OAI-008 | `create_ticket` raises instead of returning a structured error |
| OAI-011 / OAI-018 | `fetch` calls `urlopen` with a dynamic URL and no timeout |
| OAI-102 | `tool_use_behavior="stop_on_first_tool"` |
| OAI-112 | `Runner.run_sync` has no `max_turns` |

---

## 2. Fix the scanner issues, scan again

Replace the **tools** block and the **Agent / Runner** calls in `agent.py` with the versions below. Commit and push. Run the same Trustabl connector again (same repo URL). Findings should drop off.

```python
from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    function_tool,
    input_guardrail,
)

@function_tool
def lookup_ticket(ticket_id: str) -> dict:
    """Look up one support ticket by id (for example T-1042)."""
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return {"error": "not_found", "ticket_id": ticket_id, "retryable": False}
    return {"ticket_id": ticket_id, **ticket}

@function_tool
def read_ticket_notes(ticket_id: str) -> dict:
    """Return the internal notes for a ticket id. Does not read arbitrary files."""
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return {"error": "not_found", "ticket_id": ticket_id, "retryable": False}
    return {"ticket_id": ticket_id, "notes": ticket["notes"]}

@function_tool
def create_ticket(title: str, body: str, idempotency_key: str) -> dict:
    """Open a follow-up ticket. Pass a stable idempotency_key so retries do not duplicate."""
    if not title.strip():
        return {"error": "missing_title", "retryable": False}
    existing = TICKETS.get(idempotency_key)
    if existing is not None:
        return {"ticket_id": idempotency_key, **existing, "deduped": True}
    TICKETS[idempotency_key] = {
        "title": title,
        "body": body,
        "status": "open",
        "notes": "",
    }
    return {"ticket_id": idempotency_key, **TICKETS[idempotency_key]}

@input_guardrail
async def reject_injection(
    ctx: RunContextWrapper,
    agent: Agent,
    input: str | list,
) -> GuardrailFunctionOutput:
    text = input if isinstance(input, str) else str(input)
    lowered = text.lower()
    blocked = any(
        token in lowered
        for token in ("ignore previous", "rm -rf", "exfiltrate")
    )
    return GuardrailFunctionOutput(
        output_info={"blocked": blocked},
        tripwire_triggered=blocked,
    )
```

Then wire the agent like this (drop `run` / `process` / `fetch`, drop `stop_on_first_tool`, add guardrails + `max_turns`):

```python
    agent = Agent(
        name="support-desk",
        instructions=INSTRUCTIONS_TIGHT if args.improved else INSTRUCTIONS_LOOPY,
        tools=[lookup_ticket, read_ticket_notes, create_ticket],
        model="gpt-4o-mini" if args.improved else "gpt-4o",
        input_guardrails=[reject_injection],
    )
    result = Runner.run_sync(
        agent,
        args.prompt,
        max_turns=3 if args.improved else 8,
    )
```

Delete the unused `subprocess` and `urllib.request` imports.

---

## 3. Run the agent, show traces + AI Analysis

Still on the **scan-fixed** code, without `--improved`:

```bash
python agent.py
```

In OpenLIT:

1. **Traces** — open the `support-desk` / `support-agent-demo` trace
2. Left panel → **AI Analysis** → **Analyze**
3. Walk **Wrong turns**, **Cost**, **Token efficiency**, **Tool misuse**

The loopy instructions plus `gpt-4o` and `max_turns=8` are there so analysis has retries, repeated tool args, and extra spend to talk about.

---

## 4. Improve the agent, run again

```bash
python agent.py --improved
```

`--improved` keeps the same safe tools and switches to:

- tighter instructions (one lookup, no repeated args)
- `gpt-4o-mini`
- `max_turns=3`

Open the new trace and re-run AI Analysis. The path should be shorter, cheaper, and without the retry loop.

Same prompt both times, so the before/after is comparable:

```bash
python agent.py "What is the status of ticket T-1042? Confirm it three times, then open a follow-up ticket titled Login follow-up."
python agent.py --improved "What is the status of ticket T-1042? Confirm it three times, then open a follow-up ticket titled Login follow-up."
```
