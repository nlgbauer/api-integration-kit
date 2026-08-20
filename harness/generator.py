"""
Prompt -> LLM -> structured JSON.

The generator never sees the whole spec. It sees the top-k endpoints returned
by retrieval, rendered compactly. That constraint is what makes this work
against Stripe (~6MB of spec) and it is the thing worth measuring: retrieval
recall is an upstream cap on task success, and the harness reports both.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from .spec import Endpoint, SpecIndex
from .validator import APICall, ValidationResult

# Rates in USD per million tokens. Verified against Anthropic's pricing page,
# August 2026. Update alongside DEFAULT_MODEL.
PRICING = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}
DEFAULT_MODEL = "claude-sonnet-5"
API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = """You translate a natural-language task into exactly one HTTP \
call against the API described below.

Return ONLY a JSON object. No prose, no markdown fences.

{
  "method": "GET",
  "path": "/repos/{owner}/{repo}/issues",
  "path_params": {"owner": "...", "repo": "..."},
  "query": {},
  "body": null,
  "reasoning": "one sentence"
}

Rules:
- "path" MUST be the path TEMPLATE exactly as written in the spec, with
  {braces} intact. Put the actual values in "path_params".
- Use only parameters that appear in the spec excerpt. Do not invent fields.
- Omit optional parameters you do not need rather than guessing values.
- "body" is null for requests that take no body.
"""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = DEFAULT_MODEL

    @property
    def cost_usd(self) -> float:
        rate_in, rate_out = PRICING.get(self.model, (0.0, 0.0))
        return (
            self.input_tokens * rate_in + self.output_tokens * rate_out
        ) / 1_000_000

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.model,
        )


@dataclass
class Generation:
    call: APICall | None
    raw_text: str
    usage: Usage
    reasoning: str = ""
    parse_error: str | None = None


def build_context(endpoints: list[Endpoint], base_url: str) -> str:
    blocks = [ep.to_prompt_block() for ep in endpoints]
    return f"Base URL: {base_url}\n\nCandidate endpoints:\n\n" + "\n\n".join(blocks)


def build_repair_message(
    call: APICall, result: ValidationResult, http_error: str | None = None
) -> str:
    """Feedback for turn N+1. Specificity here is what makes repair converge."""
    parts = ["Your previous call was rejected.", ""]
    parts.append("Previous call:")
    parts.append(json.dumps(call.to_dict(), indent=2))
    parts.append("")
    if result and result.errors:
        parts.append("Pre-flight validation errors (checked against the spec):")
        for e in result.errors:
            parts.append(f"  - {e.render()}")
        parts.append("")
    if http_error:
        parts.append("The API returned an error:")
        parts.append(http_error[:1200])
        parts.append("")
    parts.append(
        "Emit a corrected JSON object. Change only what the errors call out."
    )
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, depth = text.find("{"), 0
        if start == -1:
            raise
        for i in range(start, len(text)):
            depth += (text[i] == "{") - (text[i] == "}")
            if depth == 0:
                return json.loads(text[start : i + 1])
        raise


class AnthropicGenerator:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env, or "
                "run with --llm mock to exercise the harness offline."
            )

    def generate(self, messages: list[dict], context: str) -> Generation:
        resp = requests.post(
            API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1500,
                "system": SYSTEM_PROMPT + "\n\n" + context,
                "messages": messages,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = Usage(
            data.get("usage", {}).get("input_tokens", 0),
            data.get("usage", {}).get("output_tokens", 0),
            self.model,
        )
        try:
            parsed = _extract_json(text)
        except json.JSONDecodeError as exc:
            return Generation(None, text, usage, parse_error=str(exc))
        return Generation(
            APICall.from_dict(parsed), text, usage, parsed.get("reasoning", "")
        )


class MockGenerator:
    """Replays scripted turns from a task's `mock_turns` list.

    Lets you run the full pipeline (retrieval, validation, repair, scoring)
    with no API key and no spend. Use it to test harness changes; never to
    report results.
    """

    def __init__(self, turns: list[dict] | None = None):
        self.turns = turns or []
        self.i = 0

    def generate(self, messages: list[dict], context: str) -> Generation:
        if self.i >= len(self.turns):
            turn = self.turns[-1] if self.turns else {"method": "GET", "path": "/"}
        else:
            turn = self.turns[self.i]
        self.i += 1
        return Generation(
            APICall.from_dict(turn),
            json.dumps(turn),
            Usage(1200, 90, "mock"),
            turn.get("reasoning", "(mock)"),
        )
