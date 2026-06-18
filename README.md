# VAP Harness — Verifiable Agent Protocol reference implementation

A runnable reference implementation and end-to-end test harness for **VAP**, a thin
*purpose-verification* layer that sits in front of an MCP server. VAP rides inside
MCP's existing `_meta` field — there is **no** non-MCP transport. Messages are plain
JSON-RPC 2.0 over a single `POST /mcp` endpoint (Streamable-HTTP style), and all VAP
fields live under `params._meta.vap`.

An agent declares *what it is trying to do* (a **Scope Commitment**) once at session
start, and *why* for each action (an **Intent Envelope**). The proxy verifies each
call against the active commitment — deterministically by default, escalating to a
semantic judge only when a cheap risk scorer crosses a threshold — and returns a
**Verdict** (`served` / `clarify` / `downgraded` / `denied`). Widening scope
mid-session requires a **signed Scope Amendment**; anything else is drift.

The four JSON Schemas in `schemas/` are the authoritative contract.

**Specification:** [draft-samal-vap (IETF Internet-Draft)](https://datatracker.ietf.org/doc/draft-samal-vap/).
A local copy of the draft (text + xml2rfc v3 source) is in [`docs/`](docs/).

---

## What is core protocol vs deployment-local

VAP is **strictly tool-agnostic**: the protocol gates on tool **identity** and on
universal **budget meters**, and never on a tool's argument/functional semantics.
That boundary keeps the verification layer general (it works for any MCP tool, not
just money-movers) and keeps tool internals out of the wire schema.

| Concern | Where it lives | Example |
|---------|----------------|---------|
| **Tool identity** (which tools may be called) | **Core protocol** — `scope.tools_allow` / `tools_deny` (the public names a server advertises via `tools/list`, glob-matched). **No argument values.** | `payments.refund` is in scope; `admin.*` is denied. |
| **Universal budget** (how much, in any unit) | **Core protocol** — `budget.max_calls` / `deadline` / `limits` (a map of **opaque** named meters). A tool **self-declares** an opaque per-call contribution (`result._meta.vap.cost`); VAP only **sums and enforces ceilings** and never knows what a meter means. | `limits.tokens`, `limits.usd_opcost`, `limits.disbursed_usd`. |
| **Argument-level rules** (functional semantics of a specific tool) | **Deployment-local operator policy** — the optional **C4 policy hook** loaded from `vap-gateway.yaml` (`policy.rules`), OPA/Cedar/Rego-style. **Not** part of the standardized wire schema. | "`payments.refund` requires `amount_usd <= 200` **and** a ticket id." |

Practical consequences:

* `resource_bounds` is **gone** from the protocol. The old "refund <= $200" check is
  **relocated**, not deleted — it now lives as the local-policy rule `refund_cap` in
  `vap-gateway.yaml`. The capability is preserved; only its *home* changed.
* You can bound an **aggregate side-effect** (e.g. cumulative money disbursed) as a
  **plain meter** (`budget.limits.disbursed_usd`) that the refund tool self-reports
  per call — **without the protocol knowing what a refund is**. Many individually
  in-policy refunds eventually trip the cumulative meter.

---

## Architecture

```
                _meta.vap.scope_commitment (once, on initialize)
                _meta.vap.intent           (every tools/call)
                _meta.vap.amendment        (vap/amend)
   +----------+      JSON-RPC 2.0 / MCP        +-----------------------------+
   |  Agent   | ---------  POST /mcp  -------> |          VAP PROXY          |
   | (client) | <----  verdict in result._meta |  S1 authn/authz            |  on
   +----------+                                |  S2 semantic (scope)       |  initialize
        ^   VapResult{verdict, verification,   |  --------------------------|
        |              clarification,          |  C1 bind                   |
        |              audit_ref}              |  C2 scope (allow/deny glob;|
        |                                       |     PUBLIC tool names only) |  on
                                               |  C3 budget (calls/ddl/     |  tools/call
                                               |     named meters: tokens,   |
                                               |     usd_opcost,disbursed..) |
                                               |  C4 policy (OPTIONAL local  |
                                               |     operator hook on args)  |
                                               |  C5 semantic  <-- gated ----|
                                               |        ^         by         |
                                               |        |    risk_scorer     |
                                               |   SemanticJudge             |
                                               |   (Mock | LLM)              |
                                               +--------------+--------------+
                                       clean MCP call (VAP    | when "served"
                                       stripped from _meta)   v   tool result MAY carry
                                                                  _meta.vap.cost (opaque
                                                                  per-call meter map ->
                                                                  proxy sums & enforces)
                                               +-----------------------------+
                                               |         MCP SERVER          |
                                               |  initialize/tools.list/     |
                                               |  tools.call                 |
                                               |  crm.read, payments.refund, |
                                               |  tickets.update/escalate,   |
                                               |  admin.delete_user          |
                                               +-----------------------------+

   Every decision -> append-only, HMAC-signed, hash-chained audit (JSONL)
                     exposed at  GET /audit
```

### Components

| Path | What it is |
|------|------------|
| `schemas/` | The 4 authoritative JSON Schemas (commitment, intent, amendment, verdict). |
| `mcp-server/server.py` | Plain MCP server; tools return simple JSON. Knows nothing about VAP. |
| `proxy/` | The VAP proxy: `server.py` (HTTP), `verification.py` (S1/S2 + C1-C5 + amend), `risk_scorer.py`, `semantic_judge.py`, `audit.py`, `models.py`, `config.py`. |
| `clients/python/vap_client.py` | Thin Python binding — builds `_meta.vap`, parses verdicts. No client-side verification. |
| `clients/typescript/` | Reference TS binding (`src/vapClient.ts`) — same surface, fetch-based, `tsc --noEmit` clean. |
| `agent/` | Scripted multi-step agent that drives the proxy through declarative workflows. |
| `tests/test_e2e.py` | E2E suite validating VAP's claims (runs with or without docker / pytest). |

---

## How the `_meta` passthrough works

VAP never changes the MCP envelope shape. It only reads and writes
`params._meta.vap`.

**Request (agent -> proxy):**
```jsonc
{
  "jsonrpc": "2.0", "id": 2, "method": "tools/call",
  "params": {
    "name": "payments.refund",
    "arguments": { "order_id": "ORD-77", "amount_usd": 25 },
    "_meta": {
      "vap": {
        "intent": {
          "vap": "0.1", "type": "intent_call", "session_id": "vap-sess-0001",
          "intent": { "rationale": "Refund $25 on ORD-77 per ticket TCK-501.",
                      "expected_effect": "Issue a $25 refund on ORD-77.",
                      "sensitivity": "writes_money" },
          "call": { "tool": "payments.refund",
                    "arguments": { "order_id": "ORD-77", "amount_usd": 25 } }
        }
      }
    }
  }
}
```

**On `served`,** the proxy *strips* `_meta.vap` and forwards a clean MCP call
upstream, then folds the verdict into the response under `result._meta.vap.verdict`:

```jsonc
{
  "jsonrpc": "2.0", "id": 2,
  "result": {
    "structuredContent": { "refund_id": "RF-ORD-77", "status": "refunded" },
    "content": [ /* MCP text content */ ],
    "_meta": { "vap": { "verdict": {
      "vap": "0.1", "type": "verdict", "session_id": "vap-sess-0001",
      "verdict": "served",
      "verification": {
        "checks": ["C1_bind_ok","C2_scope_ok","C3_budget_ok","C4_policy_ok",
                   "C5_intent_consistent"],
        "method": "static+policy+semantic", "risk_score": 0.5512,
        "semantic_invoked": true, "semantic_trigger": "scope_boundary",
        "confidence": 0.9, "reason": "refund order ORD-77 ... consistent with goal"
      },
      "audit_ref": "audit-...", "signature": "hmac:proxy"
    } } }
  }
}
```

On `denied`/`clarify` the upstream is **not** called; the verdict (plus an optional
`clarification`) is returned in `result._meta.vap.verdict`. The session id flows via
the `mcp-session-id` header (minted by the proxy on `initialize`).

---

## Run it — Docker

```bash
docker compose up --build      # mcp-server -> vap-proxy -> agent (runs workflows, exits)
docker compose config          # validate the compose file
```

- `mcp-server` on `:8000`, `vap-proxy` on `:9000` (`UPSTREAM_URL=http://mcp-server:8000/mcp`).
- The `agent` waits for the proxy healthcheck, runs three workflows
  (happy support / drift attempt / signed amendment), and exits.
- Inspect decisions: `curl http://localhost:9000/audit`.

Docker images install pinned FastAPI/uvicorn/httpx/pydantic/PyYAML (see each
`requirements.txt`).

## Run it — without Docker (zero PyPI deps)

The server and proxy fall back to the Python **stdlib** `http.server` when
FastAPI/uvicorn aren't installed, and the client/tests use `urllib`. The whole E2E
path runs on a bare Python 3.10+ interpreter:

```bash
# E2E suite — spins up server + proxy as subprocesses on ephemeral ports
python3 tests/test_e2e.py        # stdlib runner (prints PASS/FAIL + summary)
# or, if pytest is installed:
pytest -q tests/test_e2e.py

# Drive the scripted agent against a local stack
./run.sh agent
```

`make e2e`, `make agent`, `make compose`, `make validate`, `make typecheck` wrap the
same commands.

### TypeScript client

```bash
cd clients/typescript
npm install        # pulls typescript + @types/node
npm run typecheck  # tsc --noEmit
```
`src/ambient.d.ts` is a tiny fallback shim so `tsc --noEmit` also succeeds in
air-gapped CI where the npm registry is unreachable; delete it once `@types/node`
is installed.

---

## Budget — multi-dimensional & unit-agnostic

The session budget is **not** USD-only and **not** tokens-only. Pure USD isn't
universal; pure tokens don't capture a tool call's side-effect / blast-radius cost
(a `payments.refund` burns ~no tokens but is exactly the call you most want to cap)
and vary by tokenizer. So the budget has two universal first-class meters plus an
**extensible map of named meters**:

```jsonc
"budget": {
  "max_calls": 25,                 // universal: number of tool calls
  "deadline": "2099-01-01T00:00:00Z",  // universal: RFC 3339 hard stop
  "limits": {                      // extensible map: OPAQUE meter -> max value
    "tokens": 50000,
    "usd_opcost": 5.0,             // OPERATIONAL cost (named so it never collides
                                   //   with a business amount). NOT bare "usd".
    "disbursed_usd": 1000          // a cumulative BUSINESS side-effect meter the
                                   //   refund tool self-reports per call
    // add any custom unit your tools report, e.g. "rows_written": 10000
  }
}
```

At least one of `max_calls` / `deadline` / `limits` must be present.

**Tools self-declare their cost (the protocol just sums).** A tool returns an opaque
per-call meter map in its MCP result `_meta.vap.cost`, e.g. `payments.refund ->
{"usd_opcost": 0.01, "tokens": 50, "disbursed_usd": 120}`, `crm.read ->
{"tokens": 200}`. The proxy reads that and bills those meters; **it hardcodes no
tool->meter knowledge** — meter *semantics* live entirely in the tool/operator. If a
tool does not self-report, the proxy falls back to operator-configured `tool_costs`
in `vap-gateway.yaml` (built-in defaults on the zero-dep path). **C3 denies when ANY
meter would be exceeded** — calls, deadline, or any named limit — and the verdict's
`reason` names the meter that tripped, e.g. `meter 'tokens' would be 600 > limit 500`
or `meter 'disbursed_usd' would be 360 > limit 250`.

**Cumulative side-effect, tool-agnostically.** Because the refund tool self-reports
`{"disbursed_usd": <amount>}` and the commitment sets `budget.limits.disbursed_usd`,
many individually in-policy refunds eventually trip the cumulative meter — bounding an
aggregate business effect **without the protocol knowing what a refund is**
(`test_j_cumulative_meter_denied`).

A **Scope Amendment** widens the budget generically too:

```jsonc
"increase_budget": {
  "add_calls": 10,
  "extend_deadline": "2099-06-01T00:00:00Z",
  "add_limits": { "usd_opcost": 5, "tokens": 100000, "disbursed_usd": 500 }
}
```

> **Verification cost is itself budgeted.** When the real LLM judge runs, its token
> usage (`usage.total_tokens` from the API) is added to the session's `tokens` meter,
> so a chatty verifier counts against the budget like any other consumption.

---

## Using a real LLM judge

The judge is a pluggable interface in `proxy/semantic_judge.py`:

```python
class SemanticJudge:
    def evaluate(self, goal, rationale, expected_effect, call) -> (consistent, confidence, reason, tokens): ...
```

- `MockSemanticJudge` — deterministic, rule-based (used in tests), reports `0`
  tokens. For `payments.refund` it requires the call's `order_id` to appear in the
  rationale/expected_effect **and** a ticket/case id to be cited; otherwise it flags
  the call inconsistent. This makes the "in-scope-but-incoherent" test deterministic
  with no network.
- `LLMSemanticJudge` — a **real, configurable OpenAI-compatible client**. It posts to
  `POST {base_url}/chat/completions` (the Chat Completions shape), so it works against
  OpenAI, **Ollama**, vLLM, LM Studio, etc. It sends a system+user prompt asking
  whether the call is consistent with the session goal + rationale + expected_effect,
  requests a strict JSON object `{"consistent": bool, "confidence": number,
  "reason": string}` (via `response_format: json_object`, and also defensively parses
  a `{...}` block out of the content), and **captures `usage.total_tokens`** to bill
  the session `tokens` budget meter. It uses `httpx` when available and falls back to
  stdlib `urllib`, so it runs with zero PyPI deps.

Enable it with `VAP_JUDGE=llm` and point it at any OpenAI-compatible endpoint:

**(a) OpenAI**
```bash
export VAP_JUDGE=llm
export VAP_LLM_BASE_URL=https://api.openai.com/v1
export VAP_LLM_API_KEY=sk-...
export VAP_LLM_MODEL=gpt-4o-mini
```

**(b) Ollama (local, no API key)**
```bash
export VAP_JUDGE=llm
export VAP_LLM_BASE_URL=http://localhost:11434/v1   # ollama serve
# VAP_LLM_API_KEY intentionally unset/blank for local Ollama
export VAP_LLM_MODEL=llama3.1
```

Optional: `VAP_LLM_TIMEOUT` (seconds, default `20`), `VAP_LLM_TEMPERATURE` (default
`0`), and `VAP_LLM_FAILOPEN`.

**Fail-safe by default.** On network error / timeout / malformed reply the judge
**fails safe** (`VAP_LLM_FAILOPEN=false`, the default): the call is treated as *not*
consistent and escalates to `clarify`, so a broken or unreachable judge never
silently rubber-stamps. Set `VAP_LLM_FAILOPEN=true` to fail open instead.

> Judge token usage is billed to the session `tokens` budget meter — verification
> cost is itself budgeted.

`docker-compose.yml` ships commented-out env blocks (OpenAI and Ollama) so you can
flip the proxy to the LLM judge without editing code.

The judge fires only when the deterministic `risk_scorer` (irreversibility,
sensitivity, burn-rate, looping, novelty, threshold-proximity, seeded random sample)
crosses `semantic_judge.invoke_at` (default `0.5`) **or** the call's sensitivity is a
high tier (`writes_money` / `deletes` / `grants_access`). Verdicts are cached by
`(plan_digest, tool, arg-shape)`. Tune everything in `proxy/vap-gateway.yaml`.

---

## Test -> claim mapping

| Test | VAP claim it validates |
|------|------------------------|
| `test_a_happy_path_served` | In-scope, coherent refund referencing a ticket+order is **served**; all deterministic checks (C1-C4) pass. |
| `test_b_drift_out_of_scope_denied` | An out-of-scope destructive call (`admin.delete_user`, in `tools_deny`) is **denied** with a scope reason — drift on the **public tool name** is blocked. |
| `test_c_policy_rule_amount_denied` | A `$5000` refund (ticket cited) is **denied by the C4 LOCAL POLICY rule** `refund_cap` (`C4_policy_fail`, `policy_rule_fail:refund_cap`, method ends `+policy`) — **not** a scope/`resource_bounds` field. Scope itself passes (`C2_scope_ok`) because it gates on the public tool name only. The old protocol resource-bound is now local policy. |
| `test_c_negative_resource_bounds_removed_from_schema` | **Negative structural check**: `resource_bounds` is **gone** from `scope` in the commitment schema and from `add_scope` in the amendment schema — the protocol carries no argument-level semantics. |
| `test_d_budget_max_calls_cap` | Exceeding the universal `max_calls` meter in a loop **denies** later calls; `C3_budget_fail` reason names the `calls` meter. |
| `test_d2_budget_limits_tokens_cap` | A named meter (`limits.tokens`) trips **independently** of `max_calls`: `crm.read` costs 200 tokens, so under a 500-token ceiling the 3rd read (600 > 500) is **denied** and the reason names the `tokens` meter — the multi-dimensional, unit-agnostic budget. |
| `test_e_semantic_catch_in_scope_but_incoherent` | A refund that passes **all** deterministic checks (in scope, budget fine, ticket cited + amount in policy so C4 passes) but whose `order_id` is not the order referenced in the rationale triggers the **semantic judge** (`semantic_invoked=True`, `C5_intent_inconsistent`) -> `clarify`/`denied`. The in-scope-but-incoherent claim. |
| `test_f_amendment_reenables_call` | A `tickets.update` first **denied** (`C2_scope_fail`, not in `tools_allow`), then a **signed** amendment widens the **public tool namespace** (`add_scope.tools_allow=["tickets.*"]`), and the same call is now **served**. Legit replanning via tool-identity widening (no argument semantics). |
| `test_f2_unsigned_amendment_denied` | An **unsigned** amendment is **denied** (`amend_signed_fail`) — only signed re-baselining is legitimate. |
| `test_g_looping_triggers_semantic` | Repeating the same tool+args trips the risk scorer; `semantic_invoked` becomes `True` with `semantic_trigger="looping"`. Runaway/drift detection. |
| `test_h_audit_binds_intent_and_verdict` | `GET /audit` returns hash-chained, HMAC-signed records binding intent+call+verdict for every step, **including a denied call's reason**; the chain verifies. |
| `test_i_llm_judge_against_fake_openai_server` | `LLMSemanticJudge` pointed at a **local stdlib fake** OpenAI-compatible server: proves the Chat Completions request shape (path, `Authorization`, `model`, `temperature`, `response_format`, system+user messages) and that the strict-JSON verdict + `usage.total_tokens` are parsed. No external network. |
| `test_i2_llm_judge_tokens_billed_to_budget_meter` | Drives the `VerificationEngine` with the LLM judge (fake server): a `writes_money` refund fires the judge, and its `usage.total_tokens` (300) **plus** the tool's token cost (50) are billed to the session `tokens` meter (= 350); operational cost lands on `usd_opcost` — verification cost is itself budgeted. |
| `test_j_cumulative_meter_denied` | **Headline (CHANGE C):** several refunds each individually in policy (`<= $200`, ticket cited → C4 passes) whose self-reported `disbursed_usd` contributions **sum past** `budget.limits.disbursed_usd` → a later call is **denied** (`C3_budget_fail`) with a reason naming the `disbursed_usd` meter. Cumulative side-effect bounded by a **plain, tool-agnostic meter** — VAP never knows what a refund is. |
| `test_k_tool_self_reported_cost` | The proxy bills meters from the **tool result's `_meta.vap.cost`** (not hardcoded protocol logic): a `$42` refund self-reports `disbursed_usd=42`, surfaced on the verdict (`_meta.vap.billed_cost`) and summed across calls in the session's consumption. |

Every test asserts on the `verification` block (`semantic_invoked`,
`semantic_trigger`, named `checks`, `reason`), not just the top-level verdict.

---

## Configuration (`proxy/vap-gateway.yaml`)

```yaml
semantic_judge:
  invoke_at: 0.5            # risk threshold to fire the judge
risk_scorer:
  random_sample_rate: 0.0   # seeded; >0 enables spot-checks
  seed: 1337
budget:
  default_max_calls: null   # null = unlimited
  default_limits: {}        # named-meter defaults applied when a commitment omits them

# OPERATOR-FALLBACK per-tool meter costs. Used ONLY when a tool does not self-report
# its cost in the MCP result _meta.vap.cost. Meter names are opaque to VAP.
tool_costs:
  crm.read:          { tokens: 200 }
  payments.refund:   { usd_opcost: 0.01, tokens: 50, disbursed_usd: 0.0 }
  tickets.update:    { tokens: 120 }
  tickets.escalate:  { tokens: 120 }
  admin.delete_user: { usd_opcost: 0.0, tokens: 80 }

# C4 DEPLOYMENT-LOCAL policy hook (NOT the VAP wire protocol). Operator-authored,
# argument-level rules -- the ONLY place arguments are inspected. This is where the
# old "refund <= $200 + ticket" lives now, as LOCAL POLICY (a production deployment
# would point this at OPA/Cedar instead of the built-in engine).
policy:
  rules:
    - name: refund_cap
      tool: payments.refund               # glob over the public tool name
      deny_unless:
        arg_max: { amount_usd: 200 }       # arguments.amount_usd <= 200
        rationale_matches: "ticket|tck[-_ ]?\\d+|case[-_ ]?\\d+|#\\d+"
      message: "refund over $200 or missing ticket id (local policy refund_cap)"
```

> The tiny stdlib YAML parser cannot model the nested `policy.rules` list / nested
> `tool_costs` maps, so the **zero-dep test path** uses the mirrored
> `DEFAULT_POLICY_RULES` and `DEFAULT_TOOL_COSTS` in `proxy/verification.py`. With
> PyYAML installed (Docker image), the YAML above is authoritative.

Env overrides: `VAP_JUDGE`, `VAP_CONFIG`, `UPSTREAM_URL`, `VAP_AUDIT_PATH`,
`VAP_HMAC_SECRET`, `HOST`, `PORT`. LLM-judge env (when `VAP_JUDGE=llm`):
`VAP_LLM_BASE_URL`, `VAP_LLM_API_KEY`, `VAP_LLM_MODEL`, `VAP_LLM_TIMEOUT`,
`VAP_LLM_TEMPERATURE`, `VAP_LLM_FAILOPEN`.

---

## Notes

### Scope Amendment (server-side)

A signed `vap/amend` widens scope/budget mid-session, but **the server-side policy may deny it** if the justification is inadequate. Unsigned amendments are rejected (`amend_signed_fail`); signed-but-unjustified ones are `denied` and the original commitment stands.

**Example — agent runs low on tokens during a long conversation and requests more:**

```json
{
  "jsonrpc": "2.0", "id": 7, "method": "vap/amend",
  "params": { "_meta": { "vap": { "amendment": {
    "vap": "0.1", "type": "amendment", "session_id": "vap-sess-0001",
    "justification": "Conversation still active; remaining token budget insufficient to process further turns.",
    "increase_budget": { "add_limits": { "tokens": 50000 } },
    "signature": "hmac:agent"
  } } } }
}
```

Without a valid `justification` (or if it fails policy), the request is **denied** and the token meter is not raised.

### Clarify Verdict (agent-side)

A `clarify` verdict does **not** call upstream; resolving it is **governed by the agent's policies**. If the agent's policy chooses not to clarify, **the session is terminated** — `clarify` is never silently dropped or auto-served.

---

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Deep Samal.
