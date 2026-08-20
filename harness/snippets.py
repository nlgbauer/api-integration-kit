"""
Turn a verified call into something you can paste into your codebase.

This is the artifact. Everything upstream exists to make sure the code emitted
here is correct: the endpoint came from the spec, every parameter was checked
against the spec, and the call actually returned 2xx before it was rendered.

Credentials are never written into a snippet. The auth value is replaced with
an environment variable reference, because these get pasted into repos.
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlencode

from .validator import APICall

TOKEN_ENV = "API_TOKEN"


def _url(call: APICall, base_url: str) -> str:
    path = call.path
    for k, v in call.path_params.items():
        path = path.replace("{" + k + "}", quote(str(v), safe=""))
    return base_url.rstrip("/") + path


def _auth_display(auth_header: str, auth_value: str) -> tuple[str, str] | None:
    """Split an auth value into (scheme, env-var reference) for rendering."""
    if not auth_header or not auth_value:
        return None
    parts = auth_value.split(" ", 1)
    scheme = parts[0] if len(parts) == 2 else ""
    return (auth_header, f"{scheme} ".lstrip() if scheme else "")


def render(
    call: APICall,
    base_url: str,
    auth_header: str = "",
    auth_value: str = "",
    extra_headers: dict | None = None,
) -> dict[str, str]:
    url = _url(call, base_url)
    extra = dict(extra_headers or {})
    extra.pop(auth_header, None)
    auth = _auth_display(auth_header, auth_value)

    return {
        "curl": _curl(call, url, auth, extra),
        "python": _python(call, url, auth, extra),
        "javascript": _javascript(call, url, auth, extra),
    }


def _curl(call, url, auth, extra) -> str:
    full = url + (f"?{urlencode(call.query)}" if call.query else "")
    lines = [f'curl -X {call.method} "{full}"']
    if auth:
        header, prefix = auth
        lines.append(f'  -H "{header}: {prefix}${TOKEN_ENV}"')
    for k, v in extra.items():
        lines.append(f'  -H "{k}: {v}"')
    if call.body:
        lines.append('  -H "Content-Type: application/json"')
        payload = json.dumps(call.body).replace("'", "'\\''")
        lines.append(f"  -d '{payload}'")
    return " \\\n".join(lines)


def _python(call, url, auth, extra) -> str:
    lines = ["import os", "import requests", ""]
    headers = dict(extra)
    header_src = json.dumps(headers, indent=4) if headers else "{}"
    if auth:
        header, prefix = auth
        entry = f'    "{header}": f"{prefix}{{os.environ[\'{TOKEN_ENV}\']}}",'
        if headers:
            header_src = header_src[:-1].rstrip() + "\n" + entry + "\n}"
        else:
            header_src = "{\n" + entry + "\n}"
    lines.append(f"headers = {header_src}")
    lines.append("")

    args = [f'    "{url}",']
    if call.query:
        args.append(f"    params={json.dumps(call.query, indent=8)[:-1]}    }},")
    if call.body:
        args.append(f"    json={json.dumps(call.body, indent=8)[:-1]}    }},")
    args.append("    headers=headers,")
    lines.append(f"response = requests.{call.method.lower()}(")
    lines += args
    lines.append(")")
    lines.append("response.raise_for_status()")
    lines.append("print(response.json())")
    return "\n".join(lines)


def _javascript(call, url, auth, extra) -> str:
    lines = [f'const url = new URL("{url}");']
    if call.query:
        lines.append(
            f"url.search = new URLSearchParams({json.dumps(call.query, indent=2)});"
        )
    lines.append("")
    header_lines = []
    if auth:
        header, prefix = auth
        header_lines.append(f'    "{header}": `{prefix}${{process.env.{TOKEN_ENV}}}`,')
    for k, v in extra.items():
        header_lines.append(f'    "{k}": "{v}",')
    if call.body:
        header_lines.append('    "Content-Type": "application/json",')

    lines.append("const response = await fetch(url, {")
    lines.append(f'  method: "{call.method}",')
    if header_lines:
        lines.append("  headers: {")
        lines += header_lines
        lines.append("  },")
    if call.body:
        lines.append(f"  body: JSON.stringify({json.dumps(call.body, indent=2)}),")
    lines.append("});")
    lines.append("")
    lines.append("if (!response.ok) throw new Error(`HTTP ${response.status}`);")
    lines.append("const data = await response.json();")
    return "\n".join(lines)
