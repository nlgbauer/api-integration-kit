"""Execute a validated APICall against the live API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import requests

from .validator import APICall


@dataclass
class Response:
    status: int
    body: Any
    headers: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_summary(self, limit: int = 900) -> str:
        if self.error:
            return self.error
        body = (
            json.dumps(self.body)[:limit]
            if not isinstance(self.body, str)
            else self.body[:limit]
        )
        return f"HTTP {self.status}: {body}"


def execute(
    call: APICall,
    base_url: str,
    headers: dict,
    dry_run: bool = False,
    timeout: int = 45,
) -> Response:
    if dry_run:
        return Response(200, {"dry_run": True, "call": call.to_dict()}, elapsed_ms=0)

    url = base_url.rstrip("/") + call.rendered_path()
    try:
        resp = requests.request(
            call.method,
            url,
            params=call.query or None,
            json=call.body if call.body else None,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return Response(0, None, error=f"transport error: {exc}")

    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:4000]

    return Response(
        status=resp.status_code,
        body=body,
        headers=dict(resp.headers),
        elapsed_ms=int(resp.elapsed.total_seconds() * 1000),
    )
