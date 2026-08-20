"""
The main flow: a plain-English use case in, a working call out.

This is what separates the kit from the benchmark. There is no ground-truth
answer, no labeled assertion, nothing to write in advance. Success is defined
the way it is in real life: the call was accepted by the spec, it was accepted
by the API, and it came back 2xx.

Steps 4 through 7 of the pipeline live here.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import snippets
from .executor import execute
from .generator import (
    AnthropicGenerator,
    MockGenerator,
    Usage,
    build_context,
    build_repair_message,
)
from .spec import SpecIndex
from .validator import APICall, ValidationResult, validate

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class TurnTrace:
    n: int
    call: dict | None = None
    reasoning: str = ""
    parse_error: str | None = None
    preflight_ok: bool | None = None
    errors: list[dict] = field(default_factory=list)
    blocked: str | None = None
    executed: bool = False
    status: int | None = None
    api_error: str | None = None
    response_preview: Any = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SolveResult:
    use_case: str
    spec_source: str
    base_url: str
    endpoints_considered: list[dict] = field(default_factory=list)
    turns: list[TurnTrace] = field(default_factory=list)
    resolved: bool = False
    final_call: dict | None = None
    final_status: int | None = None
    response_preview: Any = None
    snippets: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _preview(body: Any, limit: int = 2400) -> Any:
    """Trim a response to something a browser can render without choking."""
    if isinstance(body, list):
        return body[:3]
    if isinstance(body, dict):
        out, size = {}, 0
        for k, v in body.items():
            chunk = repr(v)
            if size + len(chunk) > limit:
                out["..."] = f"({len(body) - len(out)} more fields)"
                break
            out[k] = v[:3] if isinstance(v, list) else v
            size += len(chunk)
        return out
    if isinstance(body, str):
        return body[:limit]
    return body


class Solver:
    def __init__(
        self,
        adapter,
        spec: SpecIndex,
        model: str = "claude-sonnet-5",
        max_turns: int = 3,
        top_k: int = 8,
        allow_writes: bool = False,
        preflight: bool = True,
        mock_turns: list[dict] | None = None,
    ):
        self.adapter = adapter
        self.spec = spec
        self.model = model
        self.max_turns = max_turns
        self.top_k = top_k
        self.allow_writes = allow_writes
        self.preflight = preflight
        self.mock_turns = mock_turns

    def inspect(self, use_case: str) -> list[dict]:
        """Step 3.5: which endpoints are even in play.

        Worth surfacing on its own. Seeing the shortlist for your use case is
        how you learn an unfamiliar API, and when the answer is wrong it tells
        you immediately whether retrieval or generation was at fault.
        """
        return [
            {
                "method": ep.method,
                "path": ep.path,
                "summary": ep.summary or ep.operation_id,
                "operation_id": ep.operation_id,
                "params": [
                    {
                        "name": p.get("name"),
                        "in": p.get("in"),
                        "required": bool(p.get("required")),
                        "type": (p.get("schema") or {}).get("type", "any"),
                        "enum": (p.get("schema") or {}).get("enum"),
                    }
                    for p in ep.parameters()[:25]
                    if p.get("name")
                ],
            }
            for ep in self.spec.retrieve(use_case, k=self.top_k)
        ]

    def solve(self, use_case: str) -> SolveResult:
        started = time.time()
        base_url = (
            self.adapter.resolve_base_url(self.spec)
            if hasattr(self.adapter, "resolve_base_url")
            else self.adapter.base_url
        )
        result = SolveResult(
            use_case=use_case, spec_source=self.spec.source, base_url=base_url
        )

        endpoints = self.spec.retrieve(use_case, k=self.top_k)
        result.endpoints_considered = self.inspect(use_case)
        context = build_context(endpoints, base_url)

        gen = (
            MockGenerator(self.mock_turns)
            if self.mock_turns is not None
            else AnthropicGenerator(model=self.model)
        )
        messages = [{"role": "user", "content": use_case}]
        total = Usage(model=self.model)

        for n in range(1, self.max_turns + 1):
            g = gen.generate(messages, context)
            total = total + g.usage
            t = TurnTrace(
                n=n,
                call=g.call.to_dict() if g.call else None,
                reasoning=g.reasoning,
                parse_error=g.parse_error,
                input_tokens=g.usage.input_tokens,
                output_tokens=g.usage.output_tokens,
            )

            if g.call is None:
                t.preflight_ok = False
                result.turns.append(t)
                messages += [
                    {"role": "assistant", "content": g.raw_text},
                    {"role": "user", "content": "That was not valid JSON. Emit only the JSON object."},
                ]
                continue

            vres = ValidationResult(ok=True)
            if self.preflight:
                vres = validate(g.call, self.spec)
                t.preflight_ok = vres.ok
                t.errors = [
                    {
                        "kind": e.kind,
                        "field": e.field,
                        "message": e.message,
                        "suggestion": e.suggestion,
                        "hallucination": e.is_hallucination,
                    }
                    for e in vres.errors
                ]

            if self.preflight and not vres.ok:
                result.turns.append(t)
                messages += [
                    {"role": "assistant", "content": g.raw_text},
                    {"role": "user", "content": build_repair_message(g.call, vres)},
                ]
                continue

            # Nothing that changes state runs without being asked for.
            if g.call.method not in READ_ONLY_METHODS and not self.allow_writes:
                t.blocked = (
                    f"{g.call.method} would change data on the target API. "
                    "The call passed validation and is shown below. Turn on "
                    "write requests to send it."
                )
                result.turns.append(t)
                result.final_call = g.call.to_dict()
                result.note = t.blocked
                result.snippets = self._snippets(g.call, base_url)
                break

            resp = execute(g.call, base_url, self.adapter.headers())
            t.executed = True
            t.status = resp.status
            t.response_preview = _preview(resp.body)
            if not resp.ok:
                t.api_error = resp.error_summary()
            result.turns.append(t)

            if resp.ok:
                result.resolved = True
                result.final_call = g.call.to_dict()
                result.final_status = resp.status
                result.response_preview = _preview(resp.body)
                result.snippets = self._snippets(g.call, base_url)
                break

            messages += [
                {"role": "assistant", "content": g.raw_text},
                {"role": "user", "content": build_repair_message(g.call, vres, resp.error_summary())},
            ]

        if not result.resolved and not result.final_call and result.turns:
            # Fall back to the last call that at least passed the spec. The
            # shape may well be right and the failure environmental (auth,
            # rate limit, a resource that does not exist), so the snippet is
            # still worth handing over as long as the note is honest.
            last = next(
                (t for t in reversed(result.turns) if t.call and t.preflight_ok),
                next((t for t in reversed(result.turns) if t.call), None),
            )
            if last:
                result.final_call = last.call
                result.final_status = last.status
                result.note = result.note or (
                    f"Did not get a 2xx in {self.max_turns} turns. "
                    + (
                        f"The last call passed spec validation but the API returned "
                        f"{last.status}, so the snippet below reflects a valid request "
                        "shape that failed for another reason."
                        if last.preflight_ok and last.status
                        else "Every attempt and rejection is shown above."
                    )
                )
                if last.preflight_ok:
                    result.snippets = self._snippets(
                        APICall.from_dict(last.call), base_url
                    )

        result.cost_usd = total.cost_usd
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    def _snippets(self, call: APICall, base_url: str) -> dict:
        return snippets.render(
            call,
            base_url,
            auth_header=getattr(self.adapter, "auth_header", ""),
            auth_value=getattr(self.adapter, "auth_value", ""),
            extra_headers=getattr(self.adapter, "extra_headers", {}),
        )
