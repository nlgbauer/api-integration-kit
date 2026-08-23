#!/usr/bin/env python3
"""
Build a single OpenAPI 3.0.1 document for the TikTok Business API.

TikTok publishes no combined spec. The tiktok/tiktok-business-api-sdk repo
carries ~200 separate per-endpoint OpenAPI documents under yml_files/
instead. This pulls the repo tarball (codeload, not the contents API --
the contents API rate-limits at 60/hour unauthenticated), extracts every
yml_files/*.yml fragment, and merges them into specs/tiktok.json.

Two things the fragments get wrong for our purposes, fixed here:
  - component schema names collide across fragments (many define their own
    "BaseResponse", etc.), so schemas are namespaced by source filename and
    every internal $ref is rewritten to match.
  - each fragment declares a bare host with no scheme; the servers block is
    overridden to the real API base URL.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import requests
import yaml

TARBALL_URL = "https://codeload.github.com/tiktok/tiktok-business-api-sdk/tar.gz/refs/heads/main"
SERVER_URL = "https://business-api.tiktok.com/open_api/v1.3"
OUT_PATH = Path(__file__).resolve().parent.parent / "specs" / "tiktok.json"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _namespace(member_name: str) -> str:
    """'tiktok-business-api-sdk-main/yml_files/tools/Region.yml' -> 'tools__Region'"""
    rel = member_name.split("yml_files/", 1)[1]
    if rel.endswith(".yml"):
        rel = rel[:-4]
    return rel.replace("/", "__")


def _rewrite_refs(node, ns: str) -> None:
    """Recursively rewrite '#/components/schemas/X' -> '#/components/schemas/{ns}__X'."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            schema_name = ref.rsplit("/", 1)[-1]
            node["$ref"] = f"#/components/schemas/{ns}__{schema_name}"
        for value in node.values():
            _rewrite_refs(value, ns)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item, ns)


def fetch_fragments() -> list[tuple[str, dict]]:
    """Download the repo tarball and return (namespace, parsed_doc) per yml_files/*.yml."""
    resp = requests.get(TARBALL_URL, timeout=120)
    resp.raise_for_status()

    fragments: list[tuple[str, dict]] = []
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if "/yml_files/" not in member.name or not member.name.endswith(".yml"):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            doc = yaml.safe_load(fh.read())
            if isinstance(doc, dict):
                fragments.append((_namespace(member.name), doc))
    return fragments


def merge(fragments: list[tuple[str, dict]]) -> dict:
    merged: dict = {
        "openapi": "3.0.1",
        "info": {
            "title": "TikTok Business API (merged from tiktok-business-api-sdk)",
            "version": "1.3",
        },
        "servers": [{"url": SERVER_URL}],
        "paths": {},
        "components": {},
    }

    for ns, doc in fragments:
        _rewrite_refs(doc, ns)

        for path, item in (doc.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            merged["paths"].setdefault(path, {}).update(item)

        for section, entries in (doc.get("components") or {}).items():
            if not isinstance(entries, dict):
                continue
            bucket = merged["components"].setdefault(section, {})
            if section == "schemas":
                for schema_name, schema_def in entries.items():
                    bucket[f"{ns}__{schema_name}"] = schema_def
            else:
                bucket.update(entries)

    return merged


def main() -> None:
    fragments = fetch_fragments()
    if not fragments:
        raise SystemExit(
            "No yml_files/*.yml fragments found in tarball -- repo layout may have changed."
        )

    merged = merge(fragments)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    op_count = sum(
        1
        for item in merged["paths"].values()
        for method in item
        if method.lower() in METHODS
    )
    print(f"Merged {len(fragments)} fragments -> {OUT_PATH}")
    print(f"  paths:      {len(merged['paths'])}")
    print(f"  operations: {op_count}")
    print(f"  schemas:    {len(merged['components'].get('schemas', {}))}")


if __name__ == "__main__":
    main()
