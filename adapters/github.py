"""GitHub REST adapter. Adapter #1 because the spec is clean and auth is a PAT.

The spec is ~10MB; SpecIndex caches it under .cache/ after the first fetch.
"""

from __future__ import annotations

from .base import Adapter


class GitHubAdapter(Adapter):
    name = "github"
    spec_url = (
        "https://raw.githubusercontent.com/github/rest-api-description/main/"
        "descriptions/api.github.com/api.github.com.json"
    )
    base_url = "https://api.github.com"
    auth_env = "GITHUB_TOKEN"

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.require_auth()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "api-integration-harness",
        }
