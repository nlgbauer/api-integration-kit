"""Adapter contract. An adapter is auth + base URL + where to find the spec.

Deliberately thin. Everything API-specific lives here; everything else in the
harness is generic. Adding an API should be ~30 lines plus an eval file.
"""

from __future__ import annotations

import os


class Adapter:
    name: str = "base"
    spec_url: str = ""
    base_url: str = ""
    auth_env: str = ""

    def headers(self) -> dict:
        raise NotImplementedError

    def require_auth(self) -> str:
        token = os.environ.get(self.auth_env)
        if not token:
            raise SystemExit(
                f"{self.auth_env} is not set. Copy .env.example to .env and fill it in, "
                f"or run with --dry-run to skip live calls."
            )
        return token
