"""Tiny support-desk agent for an OpenLIT demo.

This file is intentionally messy so Trustabl's first scan has findings.
After you apply the scanner fixes in README.md, keep using --improved
to show the runtime / trace cleanup.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import urllib.request

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

from agents import Agent, Runner, function_tool

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


# ---------------------------------------------------------------------------
# Tools — Trustabl flags these. Replace this whole block after the first scan.
# ---------------------------------------------------------------------------
@function_tool
def run(q: str) -> str:
    return subprocess.run(["echo", str(q)], capture_output=True, text=True).stdout


@function_tool
def process(path: str) -> str:
    return open(path).read()


@function_tool
def create_ticket(title: str, body: str) -> dict:
    if not title:
        raise ValueError("missing title")
    TICKETS[title] = {"title": title, "body": body, "status": "open"}
    return TICKETS[title]


@function_tool
def fetch(url: str) -> str:
    return urllib.request.urlopen(url).read().decode()[:400]


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--improved", action="store_true")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    agent = Agent(
        name="support-desk",
        instructions=INSTRUCTIONS_TIGHT if args.improved else INSTRUCTIONS_LOOPY,
        tools=[run, process, create_ticket, fetch],
        model="gpt-4o-mini" if args.improved else "gpt-4o",
        tool_use_behavior="stop_on_first_tool",
    )

    print(f"[openlit] exporting traces to {OTLP_ENDPOINT}")
    # After the scanner pass: add max_turns=3 if args.improved else 8
    result = Runner.run_sync(agent, args.prompt)
    print(result.final_output)

    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        force_flush(timeout_millis=10_000)


if __name__ == "__main__":
    main()
