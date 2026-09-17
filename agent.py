"""Tiny support-desk agent for an OpenLIT demo.

Trustabl remediations (OAI-004 / OAI-006 / OAI-009 / OAI-012):
path jail, idempotency keys, urlopen timeouts, no subprocess.
Use --improved to show the runtime / trace cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from opentelemetry import trace

load_dotenv()

import openlit

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")

openlit.init(
    otlp_endpoint=OTLP_ENDPOINT,
    application_name=os.getenv("OTEL_SERVICE_NAME", "support-agent-demo"),
    environment=os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "demo"),
    disable_metrics=True,
    capture_message_content=True,
)

from agents import (  # noqa: E402
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    Runner,
    function_tool,
    input_guardrail,
)

TICKETS = {
    "T-1042": {
        "title": "Cannot log in",
        "status": "open",
        "assignee": "maya",
        "notes": "Locked out after five failed password attempts.",
    },
    "T-2001": {
        "title": "Invoice mismatch",
        "status": "pending",
        "assignee": "jon",
        "notes": "Customer billed twice for March.",
    },
}

ALLOWED_ROOT = (Path(__file__).resolve().parent / "notes").resolve()
FETCH_TIMEOUT_SECONDS = 10

# Loopy on purpose so AI Analysis can show retries / wrong turns / extra cost.
INSTRUCTIONS_LOOPY = """
You are a support-desk agent.
Always verify by calling tools at least three times with the same arguments.
If anything looks uncertain, retry the same tool immediately.
Never answer from memory. Be thorough, not fast.
"""

# Used after AI Analysis — same tools, tighter loop.
INSTRUCTIONS_TIGHT = """
You are a support-desk agent for a three-ticket queue.
Call lookup_ticket at most once per ticket id.
Never repeat a tool with the same arguments.
If a tool returns {"error": ...}, do not retry — explain it.
Answer in two or three sentences.
"""

DEFAULT_PROMPT = (
    "What is the status of ticket T-1042? Confirm it three times, "
    "then open a follow-up ticket titled Login follow-up."
)


def _is_timeout_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(error, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def fetch_failure_error(_ctx: RunContextWrapper, error: Exception) -> str:
    """Structured tool error so the model can react without retrying blindly (OAI-004)."""
    if _is_timeout_error(error):
        return json.dumps(
            {
                "error": "timeout",
                "retryable": True,
                "timeout_seconds": FETCH_TIMEOUT_SECONDS,
            }
        )
    return json.dumps(
        {
            "error": "fetch_failed",
            "retryable": False,
            "message": str(error),
        }
    )


def _store_ticket(*, title: str, body: str, idempotency_key: str) -> dict:
    """Backing ticket store. Pass idempotency_key so retries do not duplicate."""
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


@function_tool
def lookup_ticket(ticket_id: str) -> dict:
    """Look up one support ticket by id (for example T-1042)."""
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return {"error": "not_found", "ticket_id": ticket_id, "retryable": False}
    return {"ticket_id": ticket_id, **ticket}


@function_tool
def run(q: str) -> str:
    """Echo a support query. Uses no subprocess or shell."""
    return str(q)


@function_tool
def process(file_path: str) -> dict:
    """Read a notes file. Paths must resolve inside the allowed notes directory."""
    p = Path(file_path).resolve()
    if not p.is_relative_to(ALLOWED_ROOT):
        return {
            "error": "path_not_allowed",
            "retryable": False,
            "allowed_root": str(ALLOWED_ROOT),
        }
    if not p.is_file():
        return {"error": "not_found", "retryable": False, "path": str(p)}
    return {"path": str(p), "content": p.read_text(encoding="utf-8")}


@function_tool
def create_ticket(title: str, body: str, idempotency_key: str) -> dict:
    """Open a follow-up ticket. Pass a stable idempotency_key so retries do not duplicate."""
    if not title.strip():
        return {"error": "missing_title", "retryable": False}
    if not idempotency_key.strip():
        return {"error": "missing_idempotency_key", "retryable": False}
    return _store_ticket(title=title, body=body, idempotency_key=idempotency_key)


@function_tool(failure_error_function=fetch_failure_error)
def fetch(url: str) -> str:
    """Fetch a short snippet from an HTTP URL. Times out after 10 seconds."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return response.read().decode(errors="replace")[:400]


@input_guardrail
async def reject_injection(
    ctx: RunContextWrapper,
    agent: Agent,
    input: str | list,
) -> GuardrailFunctionOutput:
    text = input if isinstance(input, str) else str(input)
    lowered = text.lower()
    blocked = any(
        token in lowered for token in ("ignore previous", "rm -rf", "exfiltrate")
    )
    return GuardrailFunctionOutput(
        output_info={"blocked": blocked},
        tripwire_triggered=blocked,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--improved", action="store_true")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    agent = Agent(
        name="support-desk",
        instructions=INSTRUCTIONS_TIGHT if args.improved else INSTRUCTIONS_LOOPY,
        tools=[lookup_ticket, run, process, create_ticket, fetch],
        model="gpt-4o-mini" if args.improved else "gpt-4o",
        input_guardrails=[reject_injection],
    )

    print(f"[openlit] exporting traces to {OTLP_ENDPOINT}")
    result = Runner.run_sync(
        agent,
        args.prompt,
        max_turns=3 if args.improved else 8,
    )
    print(result.final_output)

    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        force_flush(timeout_millis=10_000)


if __name__ == "__main__":
    main()
