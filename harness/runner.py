"""
The loop: retrieve -> generate -> pre-flight validate -> execute -> repair.

Every turn is instrumented. What separates this from a demo is that a run
produces a results file you can diff against another run: different model,
different retrieval k, pre-flight on vs off.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import assertions
from .executor import Response, execute
from .generator import (
    AnthropicGenerator,
    Generation,
    MockGenerator,
    Usage,
    build_context,
    build_repair_message,
)
from .spec import SpecIndex
from .validator import APICall, ValidationResult, validate


@dataclass
class TurnRecord:
    turn: int
    call: dict | None
    reasoning: str = ""
    parse_error: str | None = None
    preflight_ok: bool | None = None
    preflight_kinds: list[str] = field(default_factory=list)
    preflight_errors: list[str] = field(default_factory=list)
    hallucinated_fields: list[str] = field(default_factory=list)
    executed: bool = False
    http_status: int | None = None
    http_error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class TaskRecord:
    task_id: str
    prompt: str
    resolved: bool = False
    turns_used: int = 0
    retrieval_hit: bool | None = None
    assertion_detail: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    cost_usd: float = 0.0
    wall_ms: int = 0
    teardown_ok: bool | None = None

    @property
    def first_pass(self) -> bool:
        return self.resolved and self.turns_used == 1

    @property
    def hallucination_turns(self) -> int:
        return sum(1 for t in self.turns if t.hallucinated_fields)


class Runner:
    def __init__(
        self,
        adapter,
        spec: SpecIndex,
        model: str | None = None,
        max_turns: int = 3,
        top_k: int = 8,
        preflight: bool = True,
        dry_run: bool = False,
        use_mock: bool = False,
        verbose: bool = True,
    ):
        self.adapter = adapter
        self.spec = spec
        self.model = model
        self.max_turns = max_turns
        self.top_k = top_k
        self.preflight = preflight
        self.dry_run = dry_run
        self.use_mock = use_mock
        self.verbose = verbose

    def _generator(self, task: dict):
        if self.use_mock:
            return MockGenerator(task.get("mock_turns", []))
        return AnthropicGenerator(model=self.model or "claude-sonnet-5")

    def run_task(self, task: dict) -> TaskRecord:
        started = time.time()
        rec = TaskRecord(task_id=task["id"], prompt=task["prompt"])
        gen = self._generator(task)

        endpoints = self.spec.retrieve(task["prompt"], k=self.top_k)
        if task.get("expect_endpoint"):
            rec.retrieval_hit = task["expect_endpoint"] in [e.key for e in endpoints]

        context = build_context(endpoints, self.adapter.base_url)
        messages: list[dict] = [{"role": "user", "content": task["prompt"]}]
        total = Usage(model=self.model or "claude-sonnet-5")
        last_response: Response | None = None

        for turn in range(1, self.max_turns + 1):
            g: Generation = gen.generate(messages, context)
            total = total + g.usage
            tr = TurnRecord(
                turn=turn,
                call=g.call.to_dict() if g.call else None,
                reasoning=g.reasoning,
                parse_error=g.parse_error,
                input_tokens=g.usage.input_tokens,
                output_tokens=g.usage.output_tokens,
            )

            if g.call is None:
                tr.preflight_ok = False
                rec.turns.append(tr)
                messages += [
                    {"role": "assistant", "content": g.raw_text},
                    {"role": "user", "content": "That was not valid JSON. Emit only the JSON object."},
                ]
                continue

            vres = ValidationResult(ok=True)
            if self.preflight:
                vres = validate(g.call, self.spec)
                tr.preflight_ok = vres.ok
                tr.preflight_kinds = vres.kinds
                tr.preflight_errors = [e.render() for e in vres.errors]
                tr.hallucinated_fields = [e.field for e in vres.hallucinations]

            if self.preflight and not vres.ok:
                rec.turns.append(tr)
                self._log(f"    turn {turn}: preflight rejected -> {vres.kinds}")
                messages += [
                    {"role": "assistant", "content": g.raw_text},
                    {"role": "user", "content": build_repair_message(g.call, vres)},
                ]
                continue

            resp = execute(
                g.call, self.adapter.base_url, self._headers(), self.dry_run
            )
            last_response = resp
            tr.executed = True
            tr.http_status = resp.status
            if not resp.ok:
                tr.http_error = resp.error_summary()
            rec.turns.append(tr)

            if resp.ok:
                passed, detail = self._assert(task, resp)
                rec.assertion_detail = detail
                if passed:
                    rec.resolved = True
                    rec.turns_used = turn
                    self._log(f"    turn {turn}: PASS ({detail})")
                    break
                self._log(f"    turn {turn}: assertion failed ({detail})")
                messages += [
                    {"role": "assistant", "content": g.raw_text},
                    {
                        "role": "user",
                        "content": build_repair_message(
                            g.call, vres, f"Call succeeded but assertion failed: {detail}"
                        ),
                    },
                ]
            else:
                self._log(f"    turn {turn}: HTTP {resp.status}")
                messages += [
                    {"role": "assistant", "content": g.raw_text},
                    {
                        "role": "user",
                        "content": build_repair_message(g.call, vres, resp.error_summary()),
                    },
                ]

        if not rec.resolved:
            rec.turns_used = len(rec.turns)

        if task.get("teardown") and last_response is not None and not self.dry_run:
            rec.teardown_ok = self._teardown(task, last_response)

        rec.cost_usd = total.cost_usd
        rec.wall_ms = int((time.time() - started) * 1000)
        return rec

    def _headers(self) -> dict:
        # Resolved lazily: a dry run must not require live credentials.
        if self.dry_run:
            return {}
        return self.adapter.headers()

    def _assert(self, task: dict, resp: Response) -> tuple[bool, str]:
        if self.dry_run:
            # No real response to assert against. A dry run tests retrieval,
            # generation, validation and plumbing, not correctness.
            return True, "dry-run: assertions skipped"
        details = []
        for a in task.get("assert", []):
            passed, detail = assertions.check(a, resp)
            details.append(f"{a.get('type')}: {detail}")
            if not passed:
                return False, "; ".join(details)
        return True, "; ".join(details) or "no assertions"

    def _teardown(self, task: dict, resp: Response) -> bool:
        """Delete anything a write task created. Skipping this poisons the next run."""
        td = task["teardown"]
        try:
            ident = assertions._dig(resp.body, td["id_from"])
        except Exception:
            return False
        call = APICall(
            method=td["method"],
            path=td["path"],
            path_params={td["id_param"]: ident},
        )
        r = execute(call, self.adapter.base_url, self._headers(), self.dry_run)
        return r.ok or r.status == 404

    def run_suite(self, tasks: list[dict], out_path: Path | None = None) -> dict:
        records: list[TaskRecord] = []
        for i, task in enumerate(tasks, 1):
            self._log(f"[{i}/{len(tasks)}] {task['id']}: {task['prompt'][:70]}")
            records.append(self.run_task(task))

        payload = {
            "config": {
                "adapter": self.adapter.name,
                "model": "mock" if self.use_mock else (self.model or "claude-sonnet-5"),
                "preflight": self.preflight,
                "dry_run": self.dry_run,
                "top_k": self.top_k,
                "max_turns": self.max_turns,
                "spec_endpoints": len(self.spec),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            "tasks": [asdict(r) for r in records],
        }
        if out_path:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            self._log(f"\nWrote {out_path}")
        return payload

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)
