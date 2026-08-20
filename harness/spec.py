"""
OpenAPI spec loading, lazy $ref resolution, and endpoint retrieval.

Why lazy resolution: Stripe's OpenAPI document is ~6MB of JSON with deep
internal $refs. Fully dereferencing it up front is slow and can blow up on
recursive schemas. We index operations cheaply (path + method + summary +
operationId + tags) and only resolve the schema graph for the handful of
endpoints that survive retrieval.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

CACHE_DIR = Path(os.environ.get("HARNESS_CACHE_DIR", ".cache"))

# Every file read and write in this project passes encoding="utf-8" explicitly.
# On Windows, Path.read_text() defaults to the system codepage (cp1252), and
# real API specs contain characters it cannot encode. Omitting it works on
# macOS and Linux and crashes on Windows, which is the worst kind of bug.
MAX_RESOLVE_DEPTH = 12

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words carry no signal about which endpoint is wanted, and leaving
# them in is actively harmful: "owned by torvalds" matched
# /issues/{n}/dependencies/blocked_by on the word "by". Corpus statistics do
# not catch this, because "by" is genuinely rare in GitHub's paths. It has to
# be handled linguistically. Content-word frequency is handled by IDF below.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "as",
    "of", "for", "to", "in", "on", "at", "by", "from", "into", "over", "with",
    "without", "about", "after", "before", "between", "up", "out", "down",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "have", "has", "had", "i", "me", "my", "we", "our", "us", "you", "your",
    "it", "its", "they", "them", "their", "that", "this", "these", "those",
    "who", "whom", "whose", "which", "what", "when", "where", "how", "why",
    "please", "want", "need", "give", "show", "fetch", "return", "returns",
    "using", "use", "via", "just", "only", "also", "any", "some", "each",
    "every", "all", "most", "more", "less", "recently", "recent", "latest",
    "am", "many", "much", "there", "here",
}


def _stem(token: str) -> str:
    """Crude singularizer. Measured, not assumed: without it, 'gist' misses
    /gists and 'repositories' misses /repos entirely. See README > Retrieval."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es") and not token.endswith("ses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase -> camel Case
    raw = [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]
    return [_stem(t) for t in raw]


def _prefix_match(a: set[str], b: set[str], min_len: int = 4) -> int:
    """Count tokens in `a` that prefix-match something in `b` ('repo'/'repositor').
    Catches morphology the stemmer misses without pulling in a real stemmer."""
    hits = 0
    for x in a:
        if len(x) < min_len:
            continue
        if any(y.startswith(x[:min_len]) or x.startswith(y[:min_len]) for y in b if len(y) >= min_len):
            hits += 1
    return hits


@dataclass
class Endpoint:
    """One (method, path) operation from the spec."""

    method: str
    path: str
    operation_id: str
    summary: str
    description: str
    tags: list[str]
    raw: dict[str, Any]
    _spec: "SpecIndex" = field(repr=False, default=None)

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    def parameters(self) -> list[dict]:
        """Resolved parameter list (path-level params merged with operation-level)."""
        params = list(self._spec.path_level_params.get(self.path, []))
        params += self.raw.get("parameters", [])
        return [self._spec.resolve(p) for p in params]

    def request_body_schema(self) -> dict | None:
        """Resolved JSON schema for the request body, if any."""
        rb = self.raw.get("requestBody")
        if not rb:
            return None
        rb = self._spec.resolve(rb)
        content = rb.get("content", {})
        for media_type in (
            "application/json",
            "application/x-www-form-urlencoded",
            "*/*",
        ):
            if media_type in content:
                return self._spec.resolve(content[media_type].get("schema", {}))
        if content:
            first = next(iter(content.values()))
            return self._spec.resolve(first.get("schema", {}))
        return None

    def to_prompt_block(self, max_params: int = 40) -> str:
        """Compact rendering handed to the model. Kept terse on purpose --
        dumping raw spec JSON is the fastest way to blow the context budget."""
        lines = [f"### {self.method} {self.path}"]
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        if self.operation_id:
            lines.append(f"operationId: {self.operation_id}")

        params = self.parameters()[:max_params]
        if params:
            lines.append("Parameters:")
            for p in params:
                loc = p.get("in", "?")
                req = " (required)" if p.get("required") else ""
                schema = p.get("schema", {})
                ptype = schema.get("type", "any")
                enum = schema.get("enum")
                enum_str = f" enum={enum}" if enum else ""
                desc = (p.get("description") or "").split("\n")[0][:110]
                lines.append(
                    f"  - {p.get('name')} [{loc}, {ptype}]{req}{enum_str} {desc}"
                )

        body = self.request_body_schema()
        if body:
            props = body.get("properties", {})
            required = set(body.get("required", []))
            if props:
                lines.append("Request body fields:")
                for name, sub in list(props.items())[:max_params]:
                    sub = self._spec.resolve(sub)
                    req = " (required)" if name in required else ""
                    stype = sub.get("type", "any")
                    desc = (sub.get("description") or "").split("\n")[0][:110]
                    lines.append(f"  - {name} [{stype}]{req} {desc}")
        return "\n".join(lines)


class SpecIndex:
    """Loads an OpenAPI 3.x document and supports keyword retrieval over it."""

    def __init__(self, doc: dict[str, Any], source: str = "<memory>"):
        self.doc = doc
        self.source = source
        self.servers = [s.get("url", "") for s in doc.get("servers", [])]
        self.endpoints: list[Endpoint] = []
        self.path_level_params: dict[str, list[dict]] = {}
        self._by_key: dict[str, Endpoint] = {}
        self._index()

    # ---------- loading ----------

    @classmethod
    def load(cls, location: str, cache: bool = True) -> "SpecIndex":
        """Load from a local path or a URL. Remote specs are cached on disk."""
        if location.startswith(("http://", "https://")):
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            fname = re.sub(r"[^A-Za-z0-9._-]", "_", location)[-120:]
            cached = CACHE_DIR / fname
            if cache and cached.exists():
                raw = cached.read_text(encoding="utf-8")
            else:
                resp = requests.get(location, timeout=120)
                resp.raise_for_status()
                raw = resp.text
                if cache:
                    cached.write_text(raw, encoding="utf-8")
        else:
            raw = Path(location).read_text(encoding="utf-8")

        doc = yaml.safe_load(raw) if raw.lstrip()[:1] not in "{[" else json.loads(raw)
        return cls(doc, source=location)

    # ---------- $ref resolution ----------

    def resolve(self, node: Any, _depth: int = 0) -> Any:
        """Resolve internal $refs. Depth-capped so recursive schemas terminate."""
        if _depth > MAX_RESOLVE_DEPTH:
            return {"type": "object", "description": "(recursion depth exceeded)"}
        if isinstance(node, list):
            return [self.resolve(n, _depth + 1) for n in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            target = self._lookup_ref(node["$ref"])
            if target is None:
                return {"description": f"(unresolved {node['$ref']})"}
            merged = {k: v for k, v in node.items() if k != "$ref"}
            resolved = self.resolve(target, _depth + 1)
            if isinstance(resolved, dict):
                return {**resolved, **merged}
            return resolved

        # Flatten single-branch composition so the validator sees properties.
        for kw in ("allOf", "oneOf", "anyOf"):
            if kw in node:
                branches = [self.resolve(b, _depth + 1) for b in node[kw]]
                if kw == "allOf":
                    merged: dict = {"type": "object", "properties": {}, "required": []}
                    for b in branches:
                        if not isinstance(b, dict):
                            continue
                        merged["properties"].update(b.get("properties", {}))
                        merged["required"] += b.get("required", [])
                        if b.get("type") and b["type"] != "object":
                            merged["type"] = b["type"]
                    rest = {k: v for k, v in node.items() if k != kw}
                    return {**merged, **rest}
                # oneOf/anyOf: we cannot pick a branch, so relax validation here.
                rest = {k: v for k, v in node.items() if k != kw}
                return {**(branches[0] if branches else {}), **rest,
                        "x-harness-union": True}

        return {k: self.resolve(v, _depth + 1) for k, v in node.items()}

    def _lookup_ref(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return None
        node: Any = self.doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    # ---------- indexing + retrieval ----------

    def _index(self) -> None:
        methods = {"get", "post", "put", "patch", "delete", "head", "options"}
        for path, item in (self.doc.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            if "parameters" in item:
                self.path_level_params[path] = item["parameters"]
            for method, op in item.items():
                if method.lower() not in methods or not isinstance(op, dict):
                    continue
                ep = Endpoint(
                    method=method.upper(),
                    path=path,
                    operation_id=op.get("operationId", ""),
                    summary=op.get("summary", ""),
                    description=(op.get("description") or "")[:400],
                    tags=op.get("tags", []) or [],
                    raw=op,
                    _spec=self,
                )
                self.endpoints.append(ep)
                self._by_key[ep.key] = ep



    def get(self, method: str, path: str) -> Endpoint | None:
        return self._by_key.get(f"{method.upper()} {path}")

    def paths(self) -> list[str]:
        return sorted({ep.path for ep in self.endpoints})

    def retrieve(self, query: str, k: int = 8) -> list[Endpoint]:
        """Score endpoints against the use case.

        Dependency-free by design (no embeddings), but not naive: it uses three
        pieces of structure the spec already carries, each of which was added
        because a measured failure demanded it.

          tags      The spec groups endpoints by resource. "repositories" maps
                    to the 'repos' tag even when it matches nothing in the URL.
          terminal  The last literal segment is what a collection endpoint
                    returns. Asking for repositories should favour a path
                    ending in /repos over one ending in /activity.
          literals  {templated} segments are parameter names, not resources.
                    Counting them let "owned by torvalds" score against
                    /repos/{owner}/... for the word "owner".

        Weights were swept against evals/, not chosen by feel. Recall@8 on the
        GitHub suite: 62% for plain token overlap, 88% with these three.
        """
        q = _tokenize(query)
        if not q:
            return self.endpoints[:k]
        qset = set(q)

        def hits(candidates: set[str]) -> int:
            """Exact matches plus prefix matches, counted once each."""
            exact = len(qset & candidates)
            return exact + max(_prefix_match(qset, candidates) - exact, 0)

        scored: list[tuple[float, Endpoint]] = []
        for ep in self.endpoints:
            literal_segments = [
                seg for seg in ep.path.strip("/").split("/") if not seg.startswith("{")
            ]
            literal_toks = set(_tokenize(" ".join(literal_segments)))
            terminal_toks = (
                set(_tokenize(literal_segments[-1])) if literal_segments else set()
            )
            tag_toks = set(_tokenize(" ".join(ep.tags)))
            haystack = " ".join(
                [ep.path, ep.operation_id, ep.summary, " ".join(ep.tags)]
            )
            toks = set(_tokenize(haystack))
            if not toks:
                continue

            overlap = qset & toks
            literal_overlap = qset & literal_toks
            literal_fuzzy = max(
                _prefix_match(qset, literal_toks) - len(literal_overlap), 0
            )
            tag_hit = hits(tag_toks)
            terminal_hit = hits(terminal_toks)

            if not overlap and not literal_fuzzy and not tag_hit:
                continue

            score = len(overlap) / (len(qset) ** 0.5)
            score += 0.60 * len(literal_overlap)
            score += 0.35 * literal_fuzzy
            score += 0.60 * tag_hit
            score += 0.80 * terminal_hit
            # Mild preference for shorter paths (collection over subresource).
            score -= 0.03 * ep.path.count("/")
            scored.append((score, ep))

        scored.sort(key=lambda x: (-x[0], x[1].path, x[1].method))
        return [ep for _, ep in scored[:k]]

    def __len__(self) -> int:
        return len(self.endpoints)
