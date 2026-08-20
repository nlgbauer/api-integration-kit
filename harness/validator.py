"""
Pre-flight validation: check a generated API call against the OpenAPI spec
BEFORE any network I/O.

This module is the point of the project. Without it, a hallucinated parameter
is a runtime mystery. You get a 400 with a vendor-specific message and the
model has to guess. With it, you get a typed error naming the invented field
and the nearest real one, which turns repair from guessing into lookup.

Error taxonomy (see README for why the split matters):

  HALLUCINATION  - the model invented API surface that does not exist
                   unknown_path, unknown_method, unknown_param, unknown_body_field
  MALFORMED      - the surface is real, the call is wrong
                   missing_required, type_mismatch, enum_violation, unrendered_path_param

Only the first class is a model-knowledge failure. Conflating them inflates
your hallucination rate and hides what is actually breaking.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .spec import Endpoint, SpecIndex

HALLUCINATION_KINDS = {
    "unknown_path",
    "unknown_method",
    "unknown_param",
    "unknown_body_field",
}

MALFORMED_KINDS = {
    "missing_required",
    "type_mismatch",
    "enum_violation",
    "unrendered_path_param",
}

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass
class ValidationError:
    kind: str
    field: str
    message: str
    suggestion: str | None = None

    @property
    def is_hallucination(self) -> bool:
        return self.kind in HALLUCINATION_KINDS

    def render(self) -> str:
        s = f"[{self.kind}] {self.field}: {self.message}"
        if self.suggestion:
            s += f" Did you mean '{self.suggestion}'?"
        return s


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    endpoint: Endpoint | None = None

    @property
    def hallucinations(self) -> list[ValidationError]:
        return [e for e in self.errors if e.is_hallucination]

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.errors]

    def render(self) -> str:
        return "\n".join(e.render() for e in self.errors)


@dataclass
class APICall:
    """The structured call the model is asked to produce."""

    method: str
    path: str
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "APICall":
        return cls(
            method=str(d.get("method", "")).upper(),
            path=str(d.get("path", "")),
            path_params=d.get("path_params") or {},
            query=d.get("query") or {},
            body=d.get("body"),
        )

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "path_params": self.path_params,
            "query": self.query,
            "body": self.body,
        }

    def rendered_path(self) -> str:
        out = self.path
        for k, v in self.path_params.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def _type_ok(value: Any, schema: dict) -> bool:
    expected = schema.get("type")
    if not expected or expected not in _TYPE_MAP:
        return True
    if isinstance(value, bool) and expected in ("integer", "number"):
        return False
    # Query strings arrive as strings; accept coercible numerics there.
    if expected in ("integer", "number") and isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return isinstance(value, _TYPE_MAP[expected])


# Conventional aliases. String similarity cannot get from 'order' to
# 'direction' or from 'limit' to 'per_page', but those are exactly the
# substitutions a model makes when it carries a habit from one API to another.
# Each entry maps a commonly-invented name to the names APIs actually use.
PARAM_ALIASES = {
    "order": ["direction", "sort", "order_by", "ordering"],
    "order_by": ["sort", "sort_by", "order"],
    "sort_by": ["sort", "order_by", "order"],
    "limit": ["per_page", "page_size", "max_results", "count", "first", "top"],
    "max_results": ["per_page", "limit", "page_size"],
    "page_size": ["per_page", "limit", "page_len"],
    "count": ["per_page", "limit", "page_size"],
    "size": ["per_page", "limit", "page_size"],
    "offset": ["page", "start", "skip", "starting_after", "from"],
    "skip": ["offset", "page", "starting_after"],
    "cursor": ["starting_after", "after", "page_token", "next"],
    "query": ["q", "search", "filter", "query_string"],
    "search": ["q", "query", "filter"],
    "keyword": ["q", "query", "search"],
    "filter": ["q", "query", "filters"],
    "fields": ["expand", "include", "select", "properties"],
    "include": ["expand", "fields", "include_fields"],
    "start_date": ["since", "created_after", "start", "from", "after"],
    "end_date": ["until", "created_before", "end", "to", "before"],
    "from_date": ["since", "start", "after"],
    "branch": ["sha", "ref", "head", "base"],
    "user": ["username", "user_id", "login", "owner"],
    "user_id": ["username", "login", "user"],
    "repo_name": ["repo", "repository"],
    "org_name": ["org", "organization"],
    "state": ["status"],
    "status": ["state"],
    "type": ["kind", "object", "category"],
}


def _suggest(
    name: str, candidates: list[str], cutoff: float = 0.6, allow_prefix: bool = False
) -> str | None:
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=cutoff)
    if matches:
        return matches[0]

    # Conventional alias, e.g. the model wrote 'order' and the API wants
    # 'direction'. Checked against what this endpoint actually accepts.
    lowered = name.lower().lstrip("$")
    available = {c.lower(): c for c in candidates}
    for alias in PARAM_ALIASES.get(lowered, []):
        if alias in available:
            return available[alias]

    if allow_prefix and name:
        # Enum values are often abbreviations ('ascending' -> 'asc'), which
        # sequence-similarity scores below the cutoff. Prefix match catches them.
        prefixed = [
            c for c in candidates
            if c and (lowered.startswith(c.lower()) or c.lower().startswith(lowered))
        ]
        if prefixed:
            return max(prefixed, key=len)
    return None


def validate(call: APICall, spec: SpecIndex) -> ValidationResult:
    errors: list[ValidationError] = []

    # 1. Does the path template exist?
    endpoint = spec.get(call.method, call.path)
    if endpoint is None:
        all_paths = spec.paths()
        if call.path in all_paths:
            allowed = sorted(
                ep.method for ep in spec.endpoints if ep.path == call.path
            )
            errors.append(
                ValidationError(
                    "unknown_method",
                    call.method,
                    f"'{call.path}' exists but does not support {call.method}. "
                    f"Allowed: {', '.join(allowed)}.",
                )
            )
        else:
            errors.append(
                ValidationError(
                    "unknown_path",
                    call.path,
                    "No such path template in the spec.",
                    _suggest(call.path, all_paths),
                )
            )
        return ValidationResult(ok=False, errors=errors)

    params = endpoint.parameters()
    by_loc: dict[str, dict[str, dict]] = {"query": {}, "path": {}, "header": {}}
    for p in params:
        loc = p.get("in")
        if loc in by_loc and p.get("name"):
            by_loc[loc][p["name"]] = p

    # 2. Path params: every {template} must be filled.
    template_vars = set(re.findall(r"\{([^}]+)\}", call.path))
    for var in sorted(template_vars - set(call.path_params)):
        errors.append(
            ValidationError(
                "missing_required", var, f"Path parameter '{var}' was not supplied."
            )
        )
    for name in sorted(set(call.path_params) - template_vars):
        errors.append(
            ValidationError(
                "unknown_param",
                name,
                f"'{name}' is not a path parameter of {call.path}.",
                _suggest(name, sorted(template_vars)),
            )
        )
    if "{" in call.rendered_path():
        errors.append(
            ValidationError(
                "unrendered_path_param",
                call.rendered_path(),
                "Rendered path still contains an unsubstituted template variable.",
            )
        )

    # 3. Query params.
    known_query = by_loc["query"]
    for name, value in call.query.items():
        spec_param = known_query.get(name)
        if spec_param is None:
            errors.append(
                ValidationError(
                    "unknown_param",
                    name,
                    f"'{name}' is not a query parameter of {endpoint.key}.",
                    _suggest(name, sorted(known_query)),
                )
            )
            continue
        schema = spec_param.get("schema", {}) or {}
        if not _type_ok(value, schema):
            errors.append(
                ValidationError(
                    "type_mismatch",
                    name,
                    f"Expected {schema.get('type')}, got "
                    f"{type(value).__name__} ({value!r}).",
                )
            )
        enum = schema.get("enum")
        if enum and value not in enum:
            errors.append(
                ValidationError(
                    "enum_violation",
                    name,
                    f"{value!r} is not one of {enum}.",
                    _suggest(str(value), [str(e) for e in enum], allow_prefix=True),
                )
            )
    for name, p in known_query.items():
        if p.get("required") and name not in call.query:
            errors.append(
                ValidationError(
                    "missing_required", name, "Required query parameter is absent."
                )
            )

    # 4. Request body.
    body_schema = endpoint.request_body_schema()
    if call.body:
        if body_schema is None:
            errors.append(
                ValidationError(
                    "unknown_body_field",
                    "<body>",
                    f"{endpoint.key} does not accept a request body.",
                )
            )
        else:
            errors.extend(_validate_body(call.body, body_schema, spec))
    elif body_schema and body_schema.get("required"):
        for name in body_schema["required"]:
            errors.append(
                ValidationError(
                    "missing_required", name, "Required body field is absent."
                )
            )

    return ValidationResult(ok=not errors, errors=errors, endpoint=endpoint)


def _validate_body(
    body: dict, schema: dict, spec: SpecIndex, prefix: str = ""
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    props = schema.get("properties") or {}

    # A union branch or a free-form object means we cannot judge unknown fields.
    strict = bool(props) and not schema.get("x-harness-union")
    if schema.get("additionalProperties") is True:
        strict = False

    if strict:
        for name, value in body.items():
            qualified = f"{prefix}{name}"
            if name not in props:
                errors.append(
                    ValidationError(
                        "unknown_body_field",
                        qualified,
                        "Field is not in the request body schema.",
                        _suggest(name, sorted(props)),
                    )
                )
                continue
            sub = spec.resolve(props[name])
            if not _type_ok(value, sub):
                errors.append(
                    ValidationError(
                        "type_mismatch",
                        qualified,
                        f"Expected {sub.get('type')}, got {type(value).__name__}.",
                    )
                )
                continue
            enum = sub.get("enum")
            if enum and value not in enum:
                errors.append(
                    ValidationError(
                        "enum_violation",
                        qualified,
                        f"{value!r} is not one of {enum}.",
                        _suggest(str(value), [str(e) for e in enum], allow_prefix=True),
                    )
                )
            if isinstance(value, dict) and sub.get("properties"):
                errors.extend(
                    _validate_body(value, sub, spec, prefix=f"{qualified}.")
                )

    for name in schema.get("required", []) or []:
        if name not in body:
            errors.append(
                ValidationError(
                    "missing_required",
                    f"{prefix}{name}",
                    "Required body field is absent.",
                )
            )
    return errors
