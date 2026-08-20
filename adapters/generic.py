"""
The generic adapter. This is what makes step 1 an input rather than a file.

Point it at any OpenAPI document and it works out where to send requests and
how to authenticate. No vendor class, no code change.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .base import Adapter

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


class GenericAdapter(Adapter):
    name = "generic"

    def __init__(
        self,
        spec_url: str,
        base_url: str | None = None,
        auth_header: str = "Authorization",
        auth_value: str = "",
        extra_headers: dict | None = None,
        label: str | None = None,
    ):
        self.spec_url = spec_url
        self._base_url = base_url or ""
        self.auth_header = auth_header.strip() if auth_header else ""
        self.auth_value = auth_value or ""
        self.extra_headers = extra_headers or {}
        self.name = label or self._label_from(spec_url)

    @staticmethod
    def _label_from(url: str) -> str:
        host = urlparse(url).netloc or "generic"
        return host.replace("www.", "").split(".")[0] or "generic"

    @property
    def base_url(self) -> str:
        return self._base_url

    def resolve_base_url(self, spec) -> str:
        """Infer the base URL from the spec's `servers` block if not supplied.

        Specs sometimes give a relative server ("/v3") or a templated one. A
        relative value is joined onto the spec's own host, which is right often
        enough to be worth doing and always overridable.
        """
        if self._base_url:
            return self._base_url

        for server in spec.servers:
            if not server:
                continue
            if server.startswith(("http://", "https://")):
                self._base_url = server.rstrip("/")
                return self._base_url
            parsed = urlparse(self.spec_url)
            self._base_url = f"{parsed.scheme}://{parsed.netloc}{server.rstrip('/')}"
            return self._base_url

        parsed = urlparse(self.spec_url)
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        return self._base_url

    def headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "api-integration-kit"}
        h.update(self.extra_headers)
        if self.auth_header and self.auth_value:
            h[self.auth_header] = self.auth_value
        return h

    def redacted_headers(self) -> dict:
        """Headers safe to render into a shareable snippet."""
        h = self.headers()
        if self.auth_header in h:
            h[self.auth_header] = "<from environment>"
        return h
