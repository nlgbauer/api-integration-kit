# ADR-001: Target OpenAPI specs, not SDKs

**Status:** accepted
**Date:** 2026-08

## Context

The original framing was "an agent that integrates with SDKs." Adding a new API
would mean teaching the agent a new SDK.

## Problem

SDKs have no common interface. `stripe.Account.create()`, GitHub's Octokit, and
Plaid's client are each hand-written with different naming, pagination, error
types, and auth. There is no shared structure to build a generic layer on, so
"supports SDKs" degrades into N bespoke integrations wearing a trench coat,
and the abstraction earns nothing.

SDKs also cannot be validated against. There is no machine-readable statement
of what parameters `create()` accepts, so a hallucinated argument is only
discoverable at runtime.

## Decision

The universal interface is an **OpenAPI 3.x document**.

## Consequences

**Good**
- Pre-flight validation becomes possible at all. A spec is a machine-readable
  contract; an SDK signature is not. This is what the whole project rests on.
- New API = base URL + auth + spec URL. ~30 lines.
- Retrieval, validation, and repair are written once and shared.
- Errors can be typed and mapped back to a specific spec constraint, which is
  what makes the repair loop converge instead of guess.

**Bad**
- APIs without a public spec are out of scope. Acceptable: every target API
  publishes one.
- Specs drift from real behaviour. The spec says one thing, the API does
  another. Found only by executing, which is why the harness executes rather
  than stopping at generation.
- Large specs cannot fit in context. This forces a retrieval layer, which
  becomes its own measured subsystem. See README > The retrieval ceiling.
- We lose SDK conveniences (auto-pagination, retries, typed responses) and
  reimplement the parts we need.

## Alternatives rejected

- **Per-SDK adapters.** N integrations, no shared validation, no generic layer.
- **Docs-page scraping + RAG.** Prose is not a contract. You can retrieve a
  passage about a parameter but cannot mechanically verify a call against it.
- **MCP servers per API.** Solves invocation, not correctness. A hand-written
  MCP server has the same unverifiable-surface problem as an SDK. Worth
  revisiting as an *output*: generate a validated MCP server from a spec.
