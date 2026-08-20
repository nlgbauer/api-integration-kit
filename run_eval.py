#!/usr/bin/env python3
"""
CLI for the API integration harness.

  # offline smoke test: no API key, no network calls to the target API
  python run_eval.py --adapter github --llm mock --dry-run

  # real run
  python run_eval.py --adapter github

  # the A/B that produces the headline number
  python run_eval.py --adapter github --preflight     --out results/on.json
  python run_eval.py --adapter github --no-preflight  --out results/off.json
  python run_eval.py --compare results/on.json results/off.json

  # debug retrieval in isolation
  python run_eval.py --adapter github --retrieve "list open issues in a repo"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import adapters
from harness.report import compare, render, summarize
from harness.runner import Runner
from harness.spec import SpecIndex


def main() -> int:
    p = argparse.ArgumentParser(description="Spec-driven API integration eval harness")
    p.add_argument("--adapter", default="github", help="github | stripe")
    p.add_argument("--tasks", help="Path to eval YAML (default: evals/<adapter>.yaml)")
    p.add_argument("--only", help="Comma-separated task ids to run")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--llm", choices=["anthropic", "mock"], default="anthropic")
    p.add_argument("--top-k", type=int, default=8, help="Endpoints given to the model")
    p.add_argument("--max-turns", type=int, default=3, help="Repair budget per task")
    p.add_argument("--preflight", dest="preflight", action="store_true", default=True)
    p.add_argument("--no-preflight", dest="preflight", action="store_false")
    p.add_argument("--dry-run", action="store_true", help="Skip live API calls")
    p.add_argument("--out", help="Where to write the results JSON")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="Diff two result files")
    p.add_argument("--retrieve", metavar="QUERY", help="Show top-k retrieval and exit")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    if args.compare:
        print(compare(Path(args.compare[0]), Path(args.compare[1])))
        return 0

    adapter = adapters.get(args.adapter)
    print(f"Loading spec for {adapter.name} (cached after first fetch)...", file=sys.stderr)
    spec = SpecIndex.load(adapter.spec_url)
    print(f"  {len(spec)} endpoints indexed", file=sys.stderr)

    if args.retrieve:
        for i, ep in enumerate(spec.retrieve(args.retrieve, k=args.top_k), 1):
            print(f"{i:2}. {ep.key}  {ep.summary[:70]}")
        return 0

    tasks_path = Path(args.tasks or f"evals/{adapter.name}.yaml")
    if not tasks_path.exists():
        raise SystemExit(f"No eval file at {tasks_path}. Write one. See evals/github.yaml.")
    tasks = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
    if args.only:
        wanted = {t.strip() for t in args.only.split(",")}
        tasks = [t for t in tasks if t["id"] in wanted]
    if not tasks:
        raise SystemExit("No tasks selected.")

    runner = Runner(
        adapter=adapter,
        spec=spec,
        model=args.model,
        max_turns=args.max_turns,
        top_k=args.top_k,
        preflight=args.preflight,
        dry_run=args.dry_run,
        use_mock=(args.llm == "mock"),
        verbose=not args.quiet,
    )

    out = Path(args.out) if args.out else Path(
        f"results/{adapter.name}_{'pre' if args.preflight else 'nopre'}.json"
    )
    payload = runner.run_suite(tasks, out_path=out)
    print(render(summarize(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
