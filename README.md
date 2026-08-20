# API Integration Kit

LLMs are good at reading API docs and drafting integration code, but when they
get it wrong there is often no signal. The API accepts an invented parameter
and returns 200. This tool takes who you want to integrate with and what you're
trying to do, checks every generated call against the API's actual spec before
sending it, and shows you exactly what was wrong and how it corrected. You end
up with a working call and a readable trace of how it got there.

---

## The problem

Every API integration starts the same way. You have a use case. The vendor has
several hundred endpoints. The gap between those two things is a week of
reading docs, guessing parameter names, and debugging 400s.

Handing that to an LLM half works, and the failure mode is worse than it looks.
Ask for repositories sorted newest first and a model will reach for `order`,
because that's what half the APIs it has read use. GitHub calls it `direction`.
GitHub does not reject `order`. It ignores it, returns 200, and hands back the
default sort. Nothing anywhere says the parameter was fake. The model gets no
signal, the repair loop has nothing to repair against, and the bug ships.

The contract already exists in machine-readable form. Nobody checks against it
before the request goes out.

---

## What it does

```
1. who you're integrating with     paste an OpenAPI spec URL
2. what you want it to do          describe it in plain English
3. pull the docs                   fetch, cache, index the spec
4. generate                        shortlist endpoints, prompt, structured JSON
5. validate BEFORE sending         check the call against the spec
6. debug                           execute, classify what came back
7. correct and retest              feed typed errors back, up to 3 turns
                                   then hand you working code
```

```
use case ──▶ shortlist ──▶ LLM ──▶ JSON ──▶ VALIDATE ──▶ send ──▶ snippet
                            ▲                   │           │
                            └──── correct ◀─────┴───────────┘
```

Every step runs. Nothing here is a stub.

---

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # ANTHROPIC_API_KEY
python app.py                 # http://127.0.0.1:5000
```

Paste a spec URL, describe what you want, press **Build the call**.

Command line, same flow:

```bash
# see which endpoints are in play. No model call, nothing spent.
python run_solve.py --spec <spec-url> --use-case "..." --inspect-only

# build it
python run_solve.py \
  --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
  --use-case "list the 3 repos owned by torvalds, most recently pushed first" \
  --auth "Bearer $GITHUB_TOKEN"
```

No adapter file, no config. Any OpenAPI 3.x document works.

---

## What you get back

**The endpoint shortlist**, before anything is spent. Which endpoints matched
your use case, with their real parameters, required ones marked. This is how
you learn an unfamiliar API, and when an answer is wrong it tells you
immediately whether retrieval or the model was at fault.

**The correction trace.** Every attempt, what the spec rejected, what changed
on the retry. Rejected fields are struck through with the spec's correction
underneath, because seeing `order` crossed out and `direction` written in is
the whole point.

**Working code.** curl, Python, and JavaScript for the call that actually
returned 2xx. Your token is never written into a snippet; it's replaced with an
environment variable reference, because these get pasted into repos.

---

## How the correction works

The validator returns a typed error naming the invented field and the real one:

```
[unknown_param] order: 'order' is not a query parameter of
GET /users/{username}/repos. Did you mean 'direction'?
```

That's real output, not an illustration. Getting from `order` to `direction`
needs more than string similarity, which is why there's an alias table of the
substitutions models actually make when carrying a habit from one API to
another: `limit` for `per_page`, `query` for `q`, `branch` for `sha`,
`start_date` for `since`. Each is checked against what that endpoint really
accepts.

The correction message hands the model the errors, the suggestions, and its own
previous attempt, so repair is a lookup rather than another guess. Budget is 3
turns. Past that the model tends to cycle between two wrong answers.

### Error taxonomy

| Class | Kinds | Meaning |
|---|---|---|
| **Invented** | `unknown_path`, `unknown_method`, `unknown_param`, `unknown_body_field` | Surface that doesn't exist |
| **Malformed** | `missing_required`, `type_mismatch`, `enum_violation`, `unrendered_path_param` | Surface is real, the call is wrong |

Worth splitting. Conflating them inflates the hallucination rate and hides
what's actually breaking.

---

## Write requests are held back

Anything other than GET, HEAD, or OPTIONS is validated and shown to you but not
sent, unless you turn write requests on. A tool that fires POSTs at your
production API while you're exploring is not a tool you'd use twice.

---

## Specs, not SDKs

The original idea was an agent that integrates with SDKs. That doesn't work.

SDKs have no common interface, so a generic layer has nothing to build against.
More importantly, **you can't validate against an SDK.** There's no
machine-readable statement of what arguments a method accepts, so a
hallucinated argument is only findable at runtime, which is the problem this
exists to solve. An OpenAPI document is a contract, and that's what makes
checking before sending possible at all.

Cost of the choice: APIs without a public spec are out of scope, specs drift
from real behaviour, and large specs don't fit in context so endpoint shortlisting
becomes its own subsystem. Full reasoning in
[ADR-001](docs/ADR-001-openapi-not-sdks.md).

---

## Known limits

**Endpoint shortlisting is still the weak point.** It's keyword scoring with
no embeddings, and on a 1,220-endpoint spec it puts the right endpoint in the
top 8 about 88% of the time, up from 62% for plain token overlap.

The gain came from using structure the spec already carries rather than from
tuning weights. Three fixes, each forced by a measured failure:

| Fix | Failure that forced it |
|---|---|
| Function-word stopping | "owned **by** torvalds" matched `/issues/{n}/dependencies/blocked_by` |
| Tag matching | "repositories" maps to the `repos` tag even when it matches nothing in the URL |
| Literal vs templated segments | "owned" prefix-matched the parameter name `{owner}`, promoting `/repos/{owner}/...` over the right endpoint |

Terminal-segment weighting matters too: the last literal segment is what a
collection endpoint returns, so a request for repositories should favour a path
ending in `/repos` over one ending in `/activity`.

IDF weighting was tried and rejected. It lost across all 32 configurations
swept, because words common in questions ("list", "by") are rare in URLs, so
corpus statistics score them as highly informative. That is a real limitation
of the statistical approach, not a tuning failure.

**The 88% is optimistic.** Eight tasks is a small sample, and the eval prompts
turned out to be easier than natural phrasing. `gh-008` says "repositories
owned by the **user** torvalds", and that word "user" is a giveaway pointing at
`/users/{username}/repos`. Drop it, phrase it the way anyone actually would,
and the endpoint falls to rank 5. The eval was scoring a slightly easier task
than the product faces. Worth fixing by rewriting the prompts without giveaway
nouns, which will lower the number and make it mean more.

This matters because it's an upper bound: an endpoint the model never sees
can't be called. Embedding-based retrieval is the next experiment, and the
regression suite will measure whether it beats 88%.

**Stripe write requests need form encoding.** Stripe's v1 API takes
`application/x-www-form-urlencoded` with bracket notation, not JSON. Reads work
today; writes need a flatten step in the adapter.

---

## Regression suite

`evals/` holds labeled tasks with ground-truth assertions. It exists to catch
regressions when the shortlisting or validation logic changes, not as the
product.

```bash
python run_eval.py --adapter github                       # score the suite
python run_eval.py --adapter github --llm mock --dry-run  # offline, no spend
python run_eval.py --compare results/on.json results/off.json
pytest tests/ -q                                          # 17 validator tests
```

Assertions check effect, not text. A task passes when the response actually
contains what was asked for, or a follow-up confirms the resource exists.
Write tasks tear down what they create, since otherwise the second run passes
for the wrong reason.

---

## Layout

```
app.py            web UI
run_solve.py      same flow, command line
static/           the interface

harness/
  spec.py         OpenAPI load, lazy $ref resolution, endpoint shortlisting
  validator.py    pre-flight validation, error taxonomy, alias table
  generator.py    prompt construction, LLM call, cost accounting
  executor.py     HTTP execution
  solve.py        the main loop
  snippets.py     curl / Python / JavaScript output
  assertions.py   effect-based assertions   (regression suite)
  runner.py       scored eval loop          (regression suite)
  report.py       aggregation and A/B       (regression suite)

adapters/
  generic.py      any spec URL. The real entry point.
  github.py       named adapter for the regression suite
  stripe.py       named adapter for the regression suite

evals/            labeled tasks
tests/            validator unit tests
docs/             ADRs
```

## Design notes

- **Lazy `$ref` resolution.** Stripe's spec has deep recursive schemas, so
  dereferencing up front is slow and can loop. Operations are indexed cheaply
  and only resolved for endpoints that make the shortlist. Depth-capped at 12.
- **Specs are cached on disk** after first fetch, so a 12.9 MB document is
  downloaded once and indexed in about a second thereafter.
- **Credentials stay local.** Held in memory for the request, never persisted,
  never written into generated code.
- **The mock generator replays scripted turns**, so the whole pipeline runs
  with no key and no spend. For testing changes, never for reporting numbers.
