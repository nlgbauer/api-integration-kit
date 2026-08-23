#!/usr/bin/env python3
"""
Local web UI for the integration kit.

    python app.py            then open http://127.0.0.1:5000

Runs on your machine, against your credentials, and holds nothing. Specs are
cached on disk so switching between use cases on the same API is instant.
"""

from __future__ import annotations

import hmac
import os
import traceback

from flask import Flask, Response, jsonify, request, send_from_directory

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from adapters.generic import GenericAdapter
from harness.solve import Solver
from harness.spec import SpecIndex

app = Flask(__name__, static_folder="static")

SITE_USER = os.environ.get("SITE_USER", "")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")


@app.before_request
def require_auth():
    if not (SITE_USER and SITE_PASSWORD):
        return None
    auth = request.authorization
    if (auth and auth.username is not None and auth.password is not None
            and hmac.compare_digest(auth.username, SITE_USER)
            and hmac.compare_digest(auth.password, SITE_PASSWORD)):
        return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="API Integration Kit"'})


_SPECS: dict[str, SpecIndex] = {}


def load_spec(url: str) -> SpecIndex:
    if url not in _SPECS:
        _SPECS[url] = SpecIndex.load(url)
    return _SPECS[url]


def build(payload: dict) -> tuple[GenericAdapter, SpecIndex, Solver]:
    spec_url = (payload.get("spec_url") or "").strip()
    if not spec_url:
        raise ValueError("Enter the URL of an OpenAPI spec.")

    adapter = GenericAdapter(
        spec_url=spec_url,
        base_url=(payload.get("base_url") or "").strip(),
        auth_header=(payload.get("auth_header") or "Authorization").strip(),
        auth_value=(payload.get("auth_value") or "").strip(),
    )
    spec = load_spec(spec_url)
    adapter.resolve_base_url(spec)

    solver = Solver(
        adapter=adapter,
        spec=spec,
        model=payload.get("model") or "claude-sonnet-5",
        max_turns=int(payload.get("max_turns") or 3),
        top_k=int(payload.get("top_k") or 8),
        allow_writes=bool(payload.get("allow_writes")),
        preflight=payload.get("preflight", True),
    )
    return adapter, spec, solver


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/inspect")
def inspect():
    """Step 3: pull the spec and show what is in play. No model call, no spend."""
    try:
        payload = request.get_json(force=True)
        adapter, spec, solver = build(payload)
        use_case = (payload.get("use_case") or "").strip()
        return jsonify(
            {
                "ok": True,
                "base_url": adapter.base_url,
                "endpoint_count": len(spec),
                "endpoints": solver.inspect(use_case) if use_case else [],
            }
        )
    except Exception as exc:
        app.logger.error(traceback.format_exc())
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/solve")
def solve():
    """Steps 4 through 7."""
    try:
        payload = request.get_json(force=True)
        use_case = (payload.get("use_case") or "").strip()
        if not use_case:
            raise ValueError("Describe what you want the API to do.")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Add it to .env and restart."
            )
        _, _, solver = build(payload)
        return jsonify({"ok": True, "result": solver.solve(use_case).to_dict()})
    except Exception as exc:
        app.logger.error(traceback.format_exc())
        return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    print("\n  API Integration Kit  ->  http://127.0.0.1:5000\n")
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), port=5000)
