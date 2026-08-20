#!/usr/bin/env python3
"""
Build an integration from the command line.

  python run_solve.py \
    --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
    --use-case "list the 3 repos owned by torvalds, most recently pushed first" \
    --auth "Bearer $GITHUB_TOKEN"

  # see the endpoint shortlist without spending anything
  python run_solve.py --spec <url> --use-case "..." --inspect-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from adapters.generic import GenericAdapter
from harness.solve import Solver
from harness.spec import SpecIndex

DIM, BOLD, RED, GREEN, BLUE, RESET = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[34m", "\033[0m"
)


def main() -> int:
    p = argparse.ArgumentParser(description="Build an API call from a use case")
    p.add_argument("--spec", required=True, help="OpenAPI spec URL or path")
    p.add_argument("--use-case", required=True, help="What you want the API to do")
    p.add_argument("--base-url", default="", help="Override the spec's server URL")
    p.add_argument("--auth", default="", help='Auth header value, e.g. "Bearer ghp_..."')
    p.add_argument("--auth-header", default="Authorization")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--max-turns", type=int, default=3)
    p.add_argument("--allow-writes", action="store_true",
                   help="Send non-GET requests. Off by default.")
    p.add_argument("--inspect-only", action="store_true",
                   help="Show the endpoint shortlist and stop. No model call.")
    p.add_argument("--json", action="store_true", help="Emit the full trace as JSON")
    args = p.parse_args()

    adapter = GenericAdapter(
        spec_url=args.spec,
        base_url=args.base_url,
        auth_header=args.auth_header,
        auth_value=args.auth,
    )
    print(f"{DIM}Reading spec...{RESET}", file=sys.stderr)
    spec = SpecIndex.load(args.spec)
    base = adapter.resolve_base_url(spec)
    print(f"{DIM}{len(spec)} endpoints  |  base {base}{RESET}\n", file=sys.stderr)

    solver = Solver(
        adapter=adapter, spec=spec, model=args.model, max_turns=args.max_turns,
        top_k=args.top_k, allow_writes=args.allow_writes,
    )

    if args.inspect_only:
        for e in solver.inspect(args.use_case):
            print(f"{BLUE}{e['method']:<6}{RESET}{e['path']}")
            if e["summary"]:
                print(f"{DIM}      {e['summary'][:88]}{RESET}")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env.")

    result = solver.solve(args.use_case)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.resolved else 1

    for t in result.turns:
        head = f"{BOLD}Attempt {t.n}{RESET}"
        if t.preflight_ok is False:
            head += f"  {RED}rejected by the spec{RESET}"
        elif t.blocked:
            head += f"  held back"
        elif t.status:
            colour = GREEN if 200 <= t.status < 300 else RED
            head += f"  {colour}HTTP {t.status}{RESET}"
        print(head)
        if t.call:
            print(f"  {t.call['method']} {t.call['path']}")
            if t.call.get("query"):
                print(f"  query {json.dumps(t.call['query'])}")
        for e in t.errors:
            tag = f"{RED}invented{RESET}" if e["hallucination"] else "malformed"
            fix = f"  {GREEN}use {e['suggestion']}{RESET}" if e["suggestion"] else ""
            print(f"    {tag}  {e['field']}: {e['message']}{fix}")
        if t.api_error:
            print(f"    {RED}{t.api_error[:180]}{RESET}")
        if t.blocked:
            print(f"    {t.blocked}")
        print()

    if result.snippets:
        print(f"{BOLD}curl{RESET}\n{result.snippets['curl']}\n")
        print(f"{DIM}python and javascript also available with --json{RESET}")
    if result.note:
        print(f"\n{result.note}")
    print(
        f"\n{DIM}{len(result.turns)} turns  |  ${result.cost_usd:.4f}  |  "
        f"{result.elapsed_ms / 1000:.1f}s{RESET}"
    )
    return 0 if result.resolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
