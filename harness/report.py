"""
Aggregate a run into metrics, and diff two runs against each other.

The headline comparison the project exists to produce:
  run A (preflight on)  vs  run B (preflight off)
-> how many invented parameters get caught before any network call, and what
   that buys you in turns, latency, and cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from .validator import HALLUCINATION_KINDS


def summarize(payload: dict) -> dict:
    tasks = payload["tasks"]
    n = len(tasks) or 1
    resolved = [t for t in tasks if t["resolved"]]
    first_pass = [t for t in resolved if t["turns_used"] == 1]

    preflight_catches, api_catches, hallucinated_fields = 0, 0, []
    for t in tasks:
        for turn in t["turns"]:
            kinds = set(turn.get("preflight_kinds") or [])
            if kinds & HALLUCINATION_KINDS:
                preflight_catches += 1
                hallucinated_fields += turn.get("hallucinated_fields") or []
            if turn.get("http_status") and not (200 <= turn["http_status"] < 300):
                api_catches += 1

    retrieval = [t["retrieval_hit"] for t in tasks if t["retrieval_hit"] is not None]

    return {
        "config": payload["config"],
        "n_tasks": len(tasks),
        "resolved": len(resolved),
        "resolve_rate": len(resolved) / n,
        "first_pass_rate": len(first_pass) / n,
        "mean_turns_to_resolve": mean([t["turns_used"] for t in resolved]) if resolved else None,
        "retrieval_recall": (sum(retrieval) / len(retrieval)) if retrieval else None,
        "preflight_hallucination_catches": preflight_catches,
        "api_error_catches": api_catches,
        "distinct_hallucinated_fields": sorted(set(hallucinated_fields)),
        "total_cost_usd": round(sum(t["cost_usd"] for t in tasks), 4),
        "cost_per_resolved_usd": (
            round(sum(t["cost_usd"] for t in tasks) / len(resolved), 4) if resolved else None
        ),
        "mean_wall_ms": int(mean([t["wall_ms"] for t in tasks])) if tasks else 0,
        "teardown_failures": [
            t["task_id"] for t in tasks if t["teardown_ok"] is False
        ],
        "failures": [
            {"id": t["task_id"], "turns": t["turns_used"], "why": t["assertion_detail"]}
            for t in tasks
            if not t["resolved"]
        ],
    }


def render(summary: dict) -> str:
    c = summary["config"]
    pct = lambda x: "n/a" if x is None else f"{x * 100:.0f}%"
    lines = [
        "",
        "=" * 62,
        f" {c['adapter']}  |  model={c['model']}  |  preflight={c['preflight']}"
        f"{'  |  DRY RUN' if c.get('dry_run') else ''}",
        f" spec: {c['spec_endpoints']} endpoints   top_k={c['top_k']}   max_turns={c['max_turns']}",
        "=" * 62,
        f"  Tasks resolved            {summary['resolved']}/{summary['n_tasks']}  ({pct(summary['resolve_rate'])})",
        f"  First-pass (no repair)    {pct(summary['first_pass_rate'])}",
        f"  Mean turns to resolve     {summary['mean_turns_to_resolve'] or 'n/a'}",
        f"  Retrieval recall@k        {pct(summary['retrieval_recall'])}",
        "",
        f"  Hallucinations caught pre-flight   {summary['preflight_hallucination_catches']}",
        f"  Errors surfaced by the API         {summary['api_error_catches']}",
        f"  Cost / resolved task               ${summary['cost_per_resolved_usd'] or 0:.4f}",
        f"  Total cost                         ${summary['total_cost_usd']:.4f}",
        f"  Mean wall time / task              {summary['mean_wall_ms']}ms",
    ]
    if summary["distinct_hallucinated_fields"]:
        lines += ["", "  Invented fields: " + ", ".join(summary["distinct_hallucinated_fields"][:12])]
    if summary["teardown_failures"]:
        lines += ["", "  !! teardown failed (state will leak into the next run): "
                  + ", ".join(summary["teardown_failures"])]
    if summary["failures"]:
        lines += ["", "  Unresolved:"]
        lines += [f"    - {f['id']} ({f['turns']} turns) {f['why'][:80]}" for f in summary["failures"]]
    lines.append("")
    return "\n".join(lines)


def compare(path_a: Path, path_b: Path) -> str:
    a = summarize(json.loads(path_a.read_text(encoding="utf-8")))
    b = summarize(json.loads(path_b.read_text(encoding="utf-8")))
    la = f"A: preflight={a['config']['preflight']}"
    lb = f"B: preflight={b['config']['preflight']}"
    rows = [
        ("resolve rate", f"{a['resolve_rate']:.0%}", f"{b['resolve_rate']:.0%}"),
        ("first-pass rate", f"{a['first_pass_rate']:.0%}", f"{b['first_pass_rate']:.0%}"),
        ("mean turns", f"{a['mean_turns_to_resolve'] or 0:.2f}", f"{b['mean_turns_to_resolve'] or 0:.2f}"),
        ("caught pre-flight", str(a["preflight_hallucination_catches"]), str(b["preflight_hallucination_catches"])),
        ("caught by API", str(a["api_error_catches"]), str(b["api_error_catches"])),
        ("cost / resolved", f"${a['cost_per_resolved_usd'] or 0:.4f}", f"${b['cost_per_resolved_usd'] or 0:.4f}"),
        ("mean wall ms", str(a["mean_wall_ms"]), str(b["mean_wall_ms"])),
    ]
    out = ["", f"{'metric':<22}{la:<22}{lb}", "-" * 62]
    out += [f"{k:<22}{va:<22}{vb}" for k, va, vb in rows]
    out.append("")
    return "\n".join(out)
