"""
Assertions score the EFFECT of a call, not the text of it.

"Does the generated code look right" is not an eval, it is a vibe. Every task
here asserts on observable outcome: the response contains what was asked for,
or a follow-up request confirms the resource now exists.
"""

from __future__ import annotations

from typing import Any

from .executor import Response


def _dig(obj: Any, path: str) -> Any:
    """Minimal dotted path with [i] indexing: 'items[0].owner.login'."""
    cur = obj
    for part in path.split("."):
        if not part:
            continue
        while "[" in part:
            name, rest = part.split("[", 1)
            idx, part = rest.split("]", 1)
            if name:
                if not isinstance(cur, dict) or name not in cur:
                    raise KeyError(name)
                cur = cur[name]
            if not isinstance(cur, list) or int(idx) >= len(cur):
                raise KeyError(f"[{idx}]")
            cur = cur[int(idx)]
            part = part.lstrip(".")
        if part:
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(part)
            cur = cur[part]
    return cur


def check(assertion: dict, response: Response, ctx: dict | None = None) -> tuple[bool, str]:
    """Return (passed, explanation)."""
    ctx = ctx or {}
    kind = assertion.get("type")

    if kind == "status":
        want = assertion.get("value", 200)
        wants = want if isinstance(want, list) else [want]
        return response.status in wants, f"status={response.status}, want {wants}"

    if not response.ok:
        return False, f"response not ok (status={response.status})"

    if kind == "path_exists":
        try:
            _dig(response.body, assertion["path"])
            return True, f"{assertion['path']} present"
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            return False, f"{assertion['path']} missing ({exc})"

    if kind == "path_equals":
        try:
            actual = _dig(response.body, assertion["path"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            return False, f"{assertion['path']} missing ({exc})"
        want = assertion["value"]
        return actual == want, f"{assertion['path']}={actual!r}, want {want!r}"

    if kind == "count":
        target = response.body
        if assertion.get("path"):
            try:
                target = _dig(response.body, assertion["path"])
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                return False, f"{assertion['path']} missing ({exc})"
        if not isinstance(target, list):
            return False, f"expected a list, got {type(target).__name__}"
        n = len(target)
        if "min" in assertion and n < assertion["min"]:
            return False, f"len={n}, want >= {assertion['min']}"
        if "max" in assertion and n > assertion["max"]:
            return False, f"len={n}, want <= {assertion['max']}"
        if "value" in assertion and n != assertion["value"]:
            return False, f"len={n}, want {assertion['value']}"
        return True, f"len={n}"

    if kind == "all_match":
        try:
            items = _dig(response.body, assertion.get("path", ""))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            return False, f"path missing ({exc})"
        if not isinstance(items, list):
            return False, "expected a list"
        if not items:
            return False, "empty list"
        field, want = assertion["field"], assertion["value"]
        bad = [i for i in items if str(_safe(i, field)).lower() != str(want).lower()]
        return not bad, f"{len(bad)}/{len(items)} items did not match {field}={want}"

    if kind == "descending":
        try:
            items = _dig(response.body, assertion.get("path", ""))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            return False, f"path missing ({exc})"
        vals = [_safe(i, assertion["field"]) for i in items]
        vals = [v for v in vals if v is not None]
        return vals == sorted(vals, reverse=True), f"order check on {assertion['field']}"

    return False, f"unknown assertion type: {kind}"


def _safe(obj: Any, path: str) -> Any:
    try:
        return _dig(obj, path)
    except Exception:
        return None
