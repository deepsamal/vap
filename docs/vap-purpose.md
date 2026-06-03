# VAP — Purpose & USPs (one page)

**What it is:** a thin, tool-agnostic **MCP verification extension** (rides in `_meta`; no MCP spec change, no server change). *Not* a replacement protocol.

## The one-line purpose

Give an MCP server the ability to verify **why** an agent is calling — not just what and who — and to gate, audit, and bound a session on that declared purpose.

## Headline USP (the part nothing else does)

**Verifiable purpose.** The server checks that each call is consistent with a declared session goal, catching **intent-incoherence** and **in-session drift** that authorization and rate-limits structurally cannot see. Drift is defined as *escape from a committed scope/budget envelope*, not deviation from a plan — so it tolerates replanning. Widening requires a signed, re-verified amendment.

## Three objectives (ranked by how unique they are)

| # | Objective | Status | The defensible slice |
|---|-----------|--------|----------------------|
| 1 | **Verifiability** — intent verification + drift | **USP** | Semantic goal-consistency as an admission gate; drift = scope-escape. |
| 2 | **Gating** — session budget; optional functional limits | **Differentiated** (basic quotas exist elsewhere) | **Tool-agnostic, self-declared meters** that also bound *cumulative side-effect* (e.g. total disbursed) without the protocol knowing what a tool does. Functional/arg limits live in the optional local policy hook, not the wire. |
| 3 | **Auditability** | **Differentiated** (logging exists elsewhere) | **Intent-bound, verdict-inclusive, commitment-chained** audit that answers "why, within what scope, and was it justified" — a byproduct of #1, not a separate system. |

## Two objectives the original three miss

- **Adoptability** — zero-friction by design (`_meta` passthrough, graceful degradation for non-VAP clients). This is *why it can ship*, and most governance layers can't claim it.
- **Mutual accountability** — signed **verdicts**, so a *denial* is non-repudiable and auditable by the client, not just the server. (Folds under #3 but is bidirectional, which logging is not.)

## What makes it viable (not an objective, but the answer to the obvious objection)

**Cost-rationed verification.** The expensive semantic check runs once per session boundary and per-call only when a cheap risk scorer (irreversibility, burn-rate, looping, novelty, random sample) fires — and the judge's own tokens are billed to the budget. Verification pays for itself; an LLM-per-call would not.

## Explicit non-goal

**Not security/authz.** Declared intent is attacker-forgeable. VAP targets **error, drift, cost, and audit**; it is defense-in-depth, layered on top of (never replacing) authentication, authorization, and attestation.

## Naming note

Keep **VAP**. "Verifiable" already spans all three objectives (verify intent → gate on it → the verdict *is* the audit record). To avoid the "rival protocol" misread, position it in prose as the **"Verifiable Agent profile / MCP verification extension"** — reserve the word *Protocol* for the IETF draft register, not the pitch.
