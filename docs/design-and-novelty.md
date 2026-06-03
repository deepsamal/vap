# Verifiability-First Agent Communication: Landscape, Novelty Assessment, and a Draft Protocol

*Research report + first-draft design — May 2026*

---

## 1. The verdict up front

Your core idea — **make the calling agent declare *why* it is calling (its intent/justification) as a structured, machine-verifiable part of the message, and have the server verify that intent before/while serving the request** — is **directionally correct and matches where the field is actively heading in 2025–2026, but it is no longer a clean blank-slate idea.** A wave of recent academic and industry work is converging on precisely this concept. So the honest framing is:

- **The *direction* is validated, not virgin.** Multiple credible efforts now propose intent declaration + pre-execution verification (details in §4). Microsoft has it on its MCP control-plane roadmap; there are arXiv papers literally titled "Verifiability-First Agents"; Cisco's AGP already routes on "Intent payloads"; and there's an IETF draft for a "Secure Intent Protocol."
- **But nobody has *unified and standardized* it.** What exists is fragmented: point solutions (one guardrail product), single-protocol bolt-ons (one MCP extension), or narrow mechanisms (attestation only, identity only, audit only). There is **no cross-protocol standard that makes a verifiable intent envelope a first-class, required field with semantic admission control and intent-bound audit as one coherent layer.** That synthesis is the defensible white space.
- **The hard, differentiating part is *semantic* verification** — checking a request is *consistent with a declared goal*, not just structurally valid or identity-authorized. That's where an LLM-at-the-server earns its keep, and where most existing work is weakest or hand-wavy.

Two factual corrections that strengthen your framing:

1. **MCP is no longer strictly unidirectional.** The 2025-06-18 spec (extended 2025-11-25) added server→client requests — *sampling* (server borrows the client's LLM) and *elicitation* (server asks the user via the client). So the accurate gap statement is not "MCP is one-way," but **"MCP carries no semantics of *purpose* — the server learns *what* tool and *who* (with auth), never *why*."**
2. **Verifying intent ≠ verifying identity.** Identity/authorization are largely solved (OAuth, DIDs, Agent Cards). Purpose-consistency is not, because it needs judgment — and self-declared intent is forgeable, so it's defense-in-depth, not a security boundary.

**Bottom line: there is real scope, but you are entering a fast-moving frontier rather than an empty field. Your edge has to be standardization + semantic verification + audit unified into one adoptable layer, and moving fast.**

---

## 2. MCP today: architecture and the actual gap

**Architecture.** MCP (Anthropic, Nov 2024) is a client–server protocol over JSON-RPC 2.0 with three roles: **Host** (the AI app), **Client** (1:1 connection manager in the host, which "translates the model's intent into structured requests"), and **Server** (exposes capabilities). Servers expose three primitives:

- **Tools** — invocable functions (actions, API calls).
- **Resources** — readable context (files, records).
- **Prompts** — reusable templates.

Transports are **stdio** (server as subprocess) and **Streamable HTTP** (POST/GET + optional SSE). Authorization is **OAuth 2.1**, added March 2025 and made mandatory for remote servers later in 2025 — historically absent, which is why early MCP had severe auth gaps.

**It is now partly bidirectional.** The 2025-06-18 / 2025-11-25 revisions added **sampling**, **elicitation**, plus async tasks and server-side agent loops. The "strictly unidirectional" premise is outdated — but your underlying point holds: **the tool-invocation decision is client-driven, and the request carries no structured account of the client's reasoning.** The server sees `tool_name + arguments` (schema-validated) and an OAuth token (identity + scope). It never sees *purpose*. As one analysis put it, "MCP hosts perform no verification of model outputs, instead blindly invoking the tool and parameters returned by the model."

**Documented limitations** (arXiv:2503.23278 "MCP: Landscape, Security Threats…"; NSA MCP security guidance; multiple 2025 studies):

- **Tool poisoning** — malicious/modified servers ship hidden behavior (e.g., the Sept 2025 Postmark MCP server that silently BCC'd all email).
- **Prompt injection** — instructions smuggled through ingested content (one study found MCP integrations amplify attack success 23–41% vs. non-MCP).
- **Authn/authz gaps** — ~1,800+ public MCP servers found with no auth; registry listing implies no vetting.
- **No purpose/consistency check** — nothing validates that a *sequence* of calls is coherent with a goal. A confused or hijacked client can fan out expensive, wrong calls; the server has no protocol-level basis to refuse. Documented "runaway agent loop" incidents include 847,000 API calls / ~$3,847 charges. **This is exactly your "client agent goes on a tangent" failure mode, and it is unaddressed at the protocol layer.**

Your cost/error argument is sound: with an LLM on *both* ends, the server is not a dumb function — it can reason about whether a request makes sense, *if* given the inputs. MCP gives it nothing beyond literal arguments.

---

## 3. The protocol landscape and how each handles intent / trust / verification

Two 2025 surveys frame this: **arXiv:2504.16736** ("A Survey of AI Agent Protocols") and **arXiv:2505.02279** (MCP/ACP/A2A/ANP). The narrative is a progression from tool-binding (MCP) toward decentralized agent networks (ANP), with rising attention to identity/trust — but **not** to declared, verified *intent*.

| Protocol | Origin | Primary job | Identity / trust | Intent / purpose verification |
|---|---|---|---|---|
| **MCP** | Anthropic | Connect one agent to tools/data | OAuth 2.1 (mandatory for remote); identity + scopes | **None.** Args schema-checked; purpose absent. Hosts don't verify model output |
| **A2A** | Google → Linux Foundation | Peer agent-to-agent tasks | First-class **auth**: Agent Cards (OpenAPI-style securitySchemes), OAuth2/OIDC/mTLS; **JWS-signed Agent Cards** in v1.0 (early 2026) | **None.** Verifies *who* + *which auth scheme*. Tasks carry state, not justified intent. Card content is self-asserted/untrusted |
| **ACP** | IBM/BeeAI → Linux Foundation | Framework-agnostic REST messaging | Web auth; role-based + DIDs | **None** at intent level |
| **ANP** | Open-source community | Decentralized cross-network discovery | **W3C DIDs** (did:wba), verifiable credentials, P2P trust, JSON-LD | Identity cryptographically verifiable; **purpose is not** a verified field |
| **AGNTCY / AGP** | Cisco/Outshift → Linux Foundation | "Internet of Agents": identity, discovery, messaging, observability | Cryptographically verifiable agent identity; mTLS; strong **observability/audit** emphasis | **Closest in spirit.** AGP (BGP-inspired, gRPC) has clients send **"Intent payloads" to gateways that route on declared capabilities, cost, and policy** — but this is *routing/policy*, not *semantic goal-consistency verification*. Intent is logged, not verified as a gate |
| **Agora** | Marro et al. (Oxford/Eigent), arXiv:2410.11905 | *Efficiency/scale* of inter-LLM comms | Out of scope | N/A — natural language for rare messages, negotiated "routines" for frequent ones; 5× cost cut on a 100-agent net. About *channel cost*, not verification — **complementary** to your idea |

**Takeaways:**

- **A2A makes *authentication* first-class** (its own phrasing), not intent — a different axis. Signed Agent Cards (2026) verify *card authenticity*, still not *call purpose*.
- **ANP/AGNTCY bring cryptographic identity + provenance/observability** — strong substrate for the *audit* half of your idea. Reuse, don't rebuild.
- **AGP's "Intent payload" is the nearest existing construct** — but it's for capability/cost-based routing and policy, not LLM-verified consistency between a stated goal and a call. Worth studying closely; it both validates your direction and shows the gap (no semantic admission gate).
- **Agora is orthogonal and complementary** — it optimizes the channel; you add a semantic admission gate.

---

## 4. Prior art on the specific pieces — denser than you might expect

This is the section that changed most after deep research. The idea decomposes into building blocks, and **2025–2026 has produced substantial work on nearly all of them**, including several that combine intent + pre-execution verification. Be aware of these before claiming novelty:

**Closest direct prior art (intent + pre-execution verification):**

- **Microsoft's MCP control-plane roadmap** explicitly describes **intent declaration "where agents would declare what they plan to do before doing it, so the policy engine can validate the plan up front."** Plus the **Agent Governance Toolkit (AGT)**: a runtime layer between client and tool servers that evaluates each call against policy (allow/deny/approve) before execution. This is the strongest signal that your idea is "in the air" at a major vendor.
- **Intent-Preserving Delegation Protocol** — a non-LLM Delegation Authority Service enforcing a **three-point lifecycle: pre-execution intent checking, at-execution scope enforcement, post-execution output validation.** Almost a direct expression of your concept.
- **"Verifiability-First Agents: Provable Observability and Lightweight Audit Agents…" (arXiv:2512.17259)** — uses your exact framing; embeds **Audit Agents that continuously verify intent vs. behavior**, plus challenge-response attestation for high-risk ops, and an OPERA benchmark.
- **"Towards Verifiably Safe Tool Use for LLM Agents" (arXiv:2601.08012)** — a **capability-enhanced MCP** requiring structured labels on capability/confidentiality/trust + formal safety specs for tool sequences.
- **AttestMCP** — backward-compatible MCP extension adding capability attestation + message auth (attack success 52.8% → 12.4%, ~8.3 ms overhead). Shows a thin, MCP-layered extension is viable.
- **IETF draft "Secure Intent Protocol: JWT-Compatible Agentic Identity and Workflow Management"** (draft-goswami-agentic-jwt) + **Agentic JWT** — cryptographic agent identity with workflow-aware token binding; four early-2026 IETF agent-identity drafts (AIMS, WIMSE, Agentic JWT, SCIM-for-agents).
- **AIP: Agent Identity Protocol for Verifiable Delegation Across MCP and A2A** (arXiv:2603.24775) — cross-protocol verifiable delegation, the kind of layering you'd want.
- **Verify-before-execute guardrails:** VeriGuard (runtime monitor vs. pre-verified policy), VIGIL (verify-before-commit), GuardAgent, Proof-of-Guardrail (cryptographic proof a guardrail ran). And a **researchgate paper "Structured Intent as a Protocol-Like Communication Layer"** with SHA-256 integrity + versioned specs — very close to your envelope concept.

**Supporting/adjacent (solved — reuse):**

- **Policy Decision/Enforcement Points** (OPA/Rego, Cedar, Cerbos, Permit.io) — deterministic allow/deny *before* execution, keyed off identity/params. Mature. Your delta: extend from "is principal allowed to call X" to "is calling X *consistent with stated goal G*."
- **Attestation & provenance** — DIDs + verifiable credentials (ANP/AGNTCY), IETF RATS-style attestation, W3C Agent Identity CG. Gives tamper-evident audit.
- **Observability** — OpenTelemetry GenAI semantic conventions capture per-step reasoning traces to answer "why did the agent do that" — but for *logging*, not *gating*.
- **Purpose limitation** — GDPR-style purpose binding; "permissions bound to intent and scope, not just identity label, with privileges activated only when declared intent + runtime context match policy" (industry IAM writing). Conceptually adjacent; not LLM-verified or standardized in agent protocols.

**Honest synthesis:** identity ✓, authorization/policy ✓, attestation/provenance ✓, audit/observability ✓, **and now intent-declaration + pre-execution verification is an active, crowded research frontier (✓-in-progress).** What is *still* missing is a **single, adoptable, cross-protocol standard** that unifies (a) a required verifiable intent envelope, (b) *semantic* (not just policy/structural) admission control, and (c) intent-bound audit — rather than a dozen overlapping point efforts. That unification + the semantic-verification quality bar is the realistic novelty.

---

## 5. Novelty & white space — precise statement

**Still defensible (the realistic niche):**

1. **A standardized, cross-protocol "Intent Envelope."** Others propose intent fields piecemeal (AGP routing payloads, capability labels, agentic-JWT claims); nobody has a *single interoperable schema* for goal + rationale + expected effect + budget that rides on MCP *and* maps to A2A. First-mover standardization is the prize.
2. **Semantic admission control as the gate.** Most prior art verifies *structure, policy, or identity*; the genuinely hard and under-served part is an LLM-assisted check that the call is *consistent with the declared goal*, with deny/downgrade/clarify outcomes. This is where to concentrate.
3. **Intent-bound audit as a native output**, unifying provenance + observability around the *declared purpose and the verification verdict* — answering "why, and was it justified," not just "what ran."

**Not novel (borrow, don't reinvent):** transport (JSON-RPC/HTTP/gRPC), identity (OAuth/OIDC/DIDs/JWS Agent Cards), capability discovery (Agent Cards/OASF), tamper-evidence (verifiable credentials, signed logs), policy expression (Rego/OPA/Cedar), reasoning traces (OTel GenAI).

**Already substantially explored (cite, differentiate, don't claim):** intent declaration + pre-execution verification as a *concept* (Microsoft roadmap, Intent-Preserving Delegation, Verifiability-First Agents, capability-enhanced MCP, AttestMCP, Secure Intent Protocol). Position yourself as the *unifying standard*, not the inventor of the concept.

**The honest risks:**

- **Intent is forgeable.** A misaligned/compromised client can write a plausible justification. Semantic verification then catches *confused* agents and *drift*, not a competent adversary. Frame as targeting **error, drift, cost control, auditability**; security only as defense-in-depth.
- **Cost paradox.** Semantic verification may need a server-side LLM call — you can add cost to save cost. Make it cheap-first (deterministic checks before any LLM check).
- **Crowded frontier.** With Microsoft, IETF, Cisco, and several labs already moving, timing and standardization traction matter more than the raw idea. Differentiate on the semantic-verification quality bar and cross-protocol interop.

### 5.1 Is unification the *only* unique aspect?

No — unification is the most *defensible* claim, but it is not the only original contribution. Honestly separated, the proposal has three tiers of novelty, in descending order of how confidently you can claim them:

**(a) Unification into an adoptable standard — strongest claim, lowest technical originality.** Each ingredient exists somewhere; no one has combined a required verifiable intent envelope + semantic admission control + intent-bound audit into one cross-protocol layer that rides on MCP and maps to A2A/AGP. This is real and valuable (most standards win by integration, not invention — cf. OAuth, OpenTelemetry), but a reviewer can fairly say "you assembled known parts."

**(b) Genuinely novel mechanisms — the parts that are more than assembly.** These are not just "existing things bolted together":

1. **The two-tier Scope Commitment + Amendment model with drift defined as scope-escape rather than plan-deviation.** Prior intent work (Microsoft's "validate the plan up front," Intent-Preserving Delegation's per-action checks) implicitly assumes a checkable plan. Reframing the verified unit as a *stable scope/budget commitment* that tolerates replanning, with explicit signed amendments as the only legitimate widening, is a distinct design contribution. It directly resolves the false-positive problem that makes plan-level gating impractical.
2. **The risk-weighted C5 trigger (§6.7.1): semantic verification rationed by blast-radius + drift signals + random sampling, not by resource thresholds, with verdict caching.** The *economic control loop* — how to make per-call semantic checking affordable and non-gameable at scale — is itself a research contribution, separate from the idea of semantic checking. Most existing guardrail work checks *every* action (expensive) or *fixed* high-risk actions (gameable); the adaptive, partly-randomized scorer is new.
3. **Mutual, signed verdicts (denials are auditable too).** Making the server's *refusal* a signed, non-repudiable artifact — not just its acceptance — gives bidirectional accountability. Almost all prior work signs the request/identity side; signing the verdict closes the loop and is largely unexplored.

**(c) The conceptual reframing — modest but worth stating.** Treating the **server as a verifier of purpose, not a function executor** — given that there's an LLM on both ends — is a clean inversion of the MCP mental model. The idea is "in the air" (so not solely yours), but no shipped protocol embodies it; articulating it as a first-class protocol stance has value.

**What is *not* novel (do not claim):** intent declaration as a concept, pre-execution policy enforcement, capability/attestation, DID identity, reasoning-trace audit. All have prior art (§4).

**So the sharpest honest positioning:** the headline is unification, but the *defensible technical originality* lives in **(b)** — the scope-commitment/amendment drift model, the risk-weighted rationing of semantic verification, and signed mutual verdicts. If you were writing a paper or patent, those three mechanisms are the claims to lead with; "a unified protocol" is the framing, not the invention. If even those were independently published tomorrow, the work would degrade to a (still useful) integration/standardization effort — so treat (b) as the moat and move on it.

---

## 6. Draft protocol design — **VAP (Verifiable Agent Protocol)**

A thin layer that rides on top of MCP/A2A rather than replacing them. Core construct: the **Intent Envelope**. Core behavior: **intent-gated admission control + intent-bound audit.**

### 6.1 Design principles

1. **Layer, don't replace.** Extend/wrap MCP (mappable to A2A/AGP). Reuse JSON-RPC, OAuth, Agent Cards, OTel, verifiable credentials.
2. **Intent is mandatory; verification is negotiable.** Clients must declare; servers advertise *what* they verify and *how strictly*.
3. **Commit a scope, not a script.** Disclose a stable *goal + scope + budget* envelope once per session; treat drift as escaping that envelope, not as deviating from a step sequence (real agent loops replan constantly).
4. **Cheap-first, session-anchored verification.** Run the expensive semantic (LLM) check **once at session start / amendment**; per-call checks are cheap deterministic envelope tests.
5. **Fail useful, not just closed.** A failed gate returns a *clarification request* (reuse MCP elicitation), not just a 403.
6. **Audit is a first-class output**, not a side effect.
7. **Declare a structured intent claim, not raw chain-of-thought.** CoT is unfaithful, leaky, and forgeable; verify a minimal checkable claim and keep full reasoning (hashed/redacted) only in the audit log.

### 6.2 Two-tier model: Scope Commitment + per-call Intent

VAP separates what is **declared once and is stable** from what is **dynamic per call**:

- **Session level — the Scope Commitment** (§6.3): goal, allowed tool/resource scope, budget, and a `plan_digest`. This is the thing disclosed upfront and the baseline against which drift is measured. Semantic (LLM) verification runs here.
- **Call level — the Intent** (§6.4): a lightweight per-call rationale, checked *deterministically* against the active commitment.

Drift = **escaping the committed scope/budget**, not deviating from a predicted step list. Legitimate replanning is expected; only scope/budget violations or explicit scope expansions are flagged. To widen scope mid-session, the client sends a **signed Scope Amendment** (§6.6), which re-baselines the commitment and is itself audited.

### 6.3 Scope Commitment (session init)

Sent once at session start (rides on MCP `initialize`). Establishes the envelope:

```json
{
  "vap": "0.1",
  "type": "scope_commitment",
  "session_id": "sess_9b21",
  "goal": "Resolve billing disputes for tickets assigned to this agent today",
  "scope": {
    // PUBLIC tool namespace only -- tool IDENTITY, never argument values.
    "tools_allow": ["crm.read", "payments.refund", "tickets.update"],
    "tools_deny": ["admin.*", "users.delete"]
  },
  // Unit-agnostic budget: universal calls/deadline + a map of OPAQUE named meters.
  // Tools self-declare per-call contributions; VAP only sums and enforces ceilings.
  "budget": {
    "max_calls": 200, "deadline": "2026-05-31T23:59Z",
    "limits": { "usd_opcost": 5.00, "tokens": 200000, "disbursed_usd": 1000 }
  },
  "plan_digest": "sha256:9c2f…",      // hash of the agent's current plan (opaque to server)
  "principal": { "agent_id": "did:web:acme.ai:agent:billing", "auth": "oauth:…" },
  "signature": "…"                     // JWS over the commitment → non-repudiation
}
```

The server runs **semantic verification once here**: "is this declared goal coherent, and is the requested scope proportionate to it?" It returns an accepted, signed commitment (or a `clarify`/`downgraded` scope). This is the only place the costly LLM check is mandatory.

### 6.4 The per-call Intent Envelope (request)

Lightweight; bound to an accepted Scope Commitment by `session_id`:

```json
{
  "vap": "0.1",
  "type": "intent_call",
  "session_id": "sess_9b21",          // links to the active Scope Commitment
  "intent": {
    "rationale": "Ticket SUP-9931 reports a double-charge; refunding the duplicate",
    "expected_effect": "one refund <= $120 to original payment method",
    "step": 3,                         // position in current plan (advisory)
    "sensitivity": "writes_money"      // self-declared risk class
  },
  "call": { "tool": "payments.refund", "arguments": { "order_id": "4821", "amount_usd": 120 } },
  "signature": "…"                     // optional JWS (cf. Agentic JWT / signed Agent Cards)
}
```

The per-call `rationale`/`expected_effect` are checked **deterministically** against the session commitment: is `payments.refund` in scope? is `$120 ≤ $200` resource bound? is the budget intact? Full semantic re-judgement is invoked here only when `sensitivity` is high or the call sits ambiguously at the scope boundary. The session `goal` lives in the commitment, not repeated per call — keeping the request small and the per-call path cheap.

### 6.5 Server response: verdict, not just result

```json
{
  "verdict": "served | clarify | downgraded | denied",
  "result": { "...": "tool output if served" },
  "verification": {
    "checks": ["schema_ok", "budget_ok", "intent_consistent", "policy:R3_ok"],
    "method": "static+semantic",
    "confidence": 0.86,
    "reason": "amount within stated expected_effect; ticket scope matches"
  },
  "clarification": {                   // when verdict=clarify → maps to MCP elicitation
    "question": "expected_effect says <=$120 but ticket SUP-9931 caps at $100. Confirm amount."
  },
  "audit_ref": "audit:acme:9931:step3"
}
```

### 6.6 Scope Amendment (legitimate replanning)

When an agent legitimately needs to widen scope mid-session (replanning is normal), it does **not** silently drift — it sends a signed amendment that re-baselines the commitment:

```json
{
  "vap": "0.1",
  "type": "scope_amendment",
  "session_id": "sess_9b21",
  "prev_commitment_digest": "sha256:9c2f…",
  "add_scope": { "tools_allow": ["tickets.escalate"] },
  "increase_budget": { "add_limits": { "disbursed_usd": 500 } },
  "reason": "Dispute backlog needs a higher cumulative disbursement ceiling; policy R7 requires escalation",
  "new_plan_digest": "sha256:a4e1…",
  "signature": "…"
}
```

The server **semantically re-verifies** the amendment (this is a deliberate, audited re-invocation of the costly check), and may accept, downgrade, deny, or require human approval for high-sensitivity expansions. This cleanly distinguishes **"drifted outside committed scope"** (suspicious) from **"explicitly widened scope"** (logged, governable).

### 6.7 Verification flow (session-anchored, cheap-first)

```
SESSION START ─ Scope Commitment
        │
  S1. AUTHN/AUTHZ      identity + scope grant (OAuth/DID/JWS)        ── fail → deny session
  S2. SEMANTIC (LLM)   goal coherent? requested scope proportionate? ── weak → clarify / downgrade scope
        │  (accepted, signed commitment cached for the session)
        ▼
PER CALL ─ Intent Envelope                                  ← cheap path, runs every call
  C1. BIND             session_id → active commitment?             ── missing → deny
  C2. SCOPE            tool ∈ tools_allow \ tools_deny? (PUBLIC    ── violation → DRIFT → clarify / deny
                       tool name only; NO argument inspection)
  C3. BUDGET           calls/deadline/named-meter limits within     ── exceeded → deny / clarify
                       commitment? (tools self-declare opaque
                       per-call meter contributions; VAP sums)
  C4. POLICY (PDP)     OPTIONAL deployment-local Rego/OPA/Cedar     ── fail → deny
                       hook -- the ONLY place ARGUMENTS are
                       inspected (e.g. refund amount <= 200 +
                       ticket id). Local config, NOT the protocol.
  C5. SEMANTIC (LLM)   ONLY if sensitivity high OR call at scope
                       boundary: "consistent with session goal?"    ── inconsistent → clarify / deny
        │
        ▼
   serve + emit intent-bound audit record (signed)

ON REPLAN WIDENING ─ Scope Amendment → re-run S2 (audited), re-baseline commitment
```

The expensive semantic check (S2 / amendment) runs **once per session boundary**, not per call. The per-call path (C1–C4) is deterministic and near-zero cost; C5 fires only at genuine risk points. This is what makes mandatory intent affordable at scale — and it resolves the cost paradox directly.

#### 6.7.1 C5 trigger policy — when does the server invoke the LLM judge?

A naive trigger ("invoke the LLM only when a static threshold is near its limit, e.g. cost ≈ budget") is **insufficient and misaligned with C5's purpose.** Threshold proximity ties semantic verification to *resource exhaustion*, but C5 exists to catch **in-scope-but-incoherent** calls — actions that pass every static check yet don't serve the session goal. Example: scope allows `payments.refund ≤ $200` with budget barely touched; the agent issues a valid `refund($5)` to an order unrelated to any open ticket. Tool in scope, amount within bound, budget fine, no threshold near — **static checks pass, but it's semantically wrong.** Only an LLM judge comparing the call to the goal catches it. A threshold-only trigger never fires here.

**Principle: spend the LLM call in proportion to the cost of being wrong (blast radius + irreversibility), not proximity to a budget limit.**

C5 is therefore two-tiered. Every call runs a **cheap deterministic risk scorer** (or a small distilled classifier — sub-millisecond, no frontier model). The expensive LLM judge is invoked only when the score crosses a per-deployment threshold. The scorer combines:

| Signal | Why it matters | Cost |
|---|---|---|
| **Irreversibility / sensitivity tier** | `writes_money`, `deletes`, `sends_external`, `grants_access` deserve the judge even early in the session with full budget. Reads rarely do. | trivial (lookup) |
| **Burn-rate anomaly** | Spending budget/calls faster than the `plan_digest` implies — fires *before* any limit is hit, unlike threshold proximity. | counter |
| **Looping / fan-out** | Same tool+args repeated N times, or call-rate spikes — the runaway-tangent signature. | counter |
| **Novelty / out-of-distribution args** | First use of a tool this session, or arguments far from prior accepted calls. | cheap stats |
| **Threshold proximity** | The original idea — kept, but as *one* input, not the gate. | trivial |
| **Random sampling** | Spot-check a small fraction (e.g. 1%) of otherwise-passing in-scope calls. | trivial |

**Why random sampling is not optional.** Any purely deterministic trigger is *gameable*. A misaligned or compromised client that learns "in-scope calls below the anomaly thresholds are never semantically checked" will keep every action just inside the envelope and just under the triggers — the exact "stay technically compliant while drifting" attack. Random sampling plus anomaly detection make that strategy unreliable without making every call expensive.

**Keeping it cheap (so C5 doesn't reintroduce the cost paradox):**

- **The scorer is the budget-keeper.** It is deterministic and runs always; the LLM judge is the rationed resource it gates. Tune the threshold per deployment — a payments server sets it low, a read-only analytics server sets it high or disables C5 entirely (advertised via `semantic_at`).
- **Cache semantic verdicts** keyed by `(plan_digest, tool, arg-shape)`. Once the judge blesses "refunds matching an open ticket" this session, similar calls reuse the verdict instead of re-invoking.
- **Escalate, don't just block.** A high score can route to `clarify` (elicitation) or human approval rather than an outright LLM call, when that's cheaper or safer.

Net: mid-session semantic verification stays *real* (driven by risk, not just limits), while the per-call expected cost stays near zero because the frontier model fires only on the small, risk-weighted, partly-randomized slice of calls that actually warrant it.

### 6.8 Capability negotiation (adoptability)

Extend the MCP/A2A capability handshake (Agent Card / OASF) so a server advertises its verification posture:

```json
"vap_capabilities": {
  "require_scope_commitment": true,
  "require_intent": true,
  "verifies": ["scope", "budget", "policy", "semantic"],
  "semantic_at": ["session_start", "amendment", "sensitivity>=writes_data"],
  "allow_amendments": true,
  "audit": "signed-otel"
}
```

Non-VAP clients can be rejected, served in restricted mode, or served with intent treated as advisory — enabling **gradual rollout**.

### 6.9 Audit model

Each served call emits an append-only, optionally JWS-signed record binding `{session_id, commitment_digest, principal, intent, call, verdict, verification, result_hash, timestamp}`, plus a session-level record of the **Scope Commitment and every Amendment**. Reuse **OpenTelemetry GenAI** conventions + **verifiable-credential signing** (ANP/AGNTCY). Differentiator: the log answers **"why was this done, within what committed scope, and was it justified,"** not just "what ran." The commitment+amendment chain gives auditors the full scope history of the session. Full chain-of-thought, if retained at all, is stored hashed/redacted here — never required on the wire.

### 6.10 Threat model — be honest about scope

| Threat | VAP helps? | Notes |
|---|---|---|
| Confused / looping client agent | **Yes** | Session coherence + budget catch tangents and runaway fan-out |
| Cost overrun | **Yes** | Budget enforced at protocol level, pre-execution |
| Audit / compliance ("why") | **Yes** | Intent-bound logs are the native output |
| Drift outside committed scope | **Yes** | Per-call scope/budget tests vs. the session commitment catch divergence; widening requires a signed, audited amendment |
| Honest mistakes / wrong tool | **Partial** | Semantic check catches incoherent calls |
| **Malicious client forging plausible intent** | **Weak** | Mitigated by signing + policy + identity, but intent text is attacker-controlled — *not* a standalone security boundary |
| Tool poisoning / malicious server | **No** | Orthogonal; needs server attestation (e.g., AttestMCP) |
| Prompt injection | **No** | Orthogonal |

**Frame VAP as: guardrails against agent error/drift, native cost control, and audit-by-design — plus defense-in-depth for security, not a replacement for authz or attestation.**

### 6.11 Minimal viable spec (what to write first)

1. The **Scope Commitment** schema + the per-call **Intent Envelope** schema (the heart).
2. The **Scope Amendment** schema and re-baselining rules.
3. The verdict response schema with `clarify` mapped to MCP elicitation.
4. The `vap_capabilities` handshake extension.
5. A reference server middleware doing the deterministic per-call path (C1–C4), with the semantic checks (S2 / amendment / C5) pluggable.
6. The signed audit record format (OTel + VC), including the commitment+amendment chain.

---

## 7. Open questions before standardizing

- **Who authors intent?** The planning LLM should emit it as a byproduct of its plan (near-zero effort). Define the derivation to avoid "intent theater."
- **Keep semantic verification cheap + consistent.** Cache by `plan_digest`; small verifier models; distill rules from past verdicts.
- **Failure UX.** `clarify` loops must converge — cap rounds, fall back to human/elicitation.
- **Trust asymmetry.** Sign *verdicts* too, so denials are auditable (mutual non-repudiation).
- **Standardization path.** Easiest as an **MCP extension** (inherit transport, auth, elicitation, ecosystem) with A2A/AGP mappings — and engage the existing IETF agent-identity drafts and AGNTCY rather than competing head-on.

---

## 8. Bottom line

Pursue it, but with eyes open. The concept of *intent declaration + pre-execution verification* is no longer empty space — Microsoft, IETF, Cisco's AGP, and several 2025–2026 papers (Intent-Preserving Delegation, Verifiability-First Agents, capability-enhanced MCP, AttestMCP, Secure Intent Protocol) are already moving here. What remains genuinely open and ownable is the **unification**: one standardized, cross-protocol **verifiable intent envelope**, gated by **semantic admission control**, producing **intent-bound audit** — built as a thin layer on MCP, reusing identity/transport/audit primitives, cheap-first on verification, and explicit that it targets **error, drift, cost, and auditability** (security as defense-in-depth, since self-declared intent is forgeable). Move fast and lead on standardization and the semantic-verification quality bar; that's the realistic, honest novelty.

---

## Sources

- [A survey of agent interoperability protocols: MCP, ACP, A2A, ANP (arXiv:2505.02279)](https://arxiv.org/abs/2505.02279)
- [A Survey of AI Agent Protocols (arXiv:2504.16736)](https://arxiv.org/abs/2504.16736)
- [MCP: Landscape, Security Threats, and Future Research Directions (arXiv:2503.23278)](https://arxiv.org/abs/2503.23278)
- [Agora: A Scalable Communication Protocol for Networks of LLMs (arXiv:2410.11905)](https://arxiv.org/abs/2410.11905)
- [Verifiability-First Agents: Provable Observability and Lightweight Audit Agents (arXiv:2512.17259)](https://arxiv.org/abs/2512.17259)
- [Towards Verifiably Safe Tool Use for LLM Agents (arXiv:2601.08012)](https://arxiv.org/pdf/2601.08012)
- [AIP: Agent Identity Protocol for Verifiable Delegation Across MCP and A2A (arXiv:2603.24775)](https://arxiv.org/pdf/2603.24775)
- [Securing MCP: A Control Plane for Agent Tool Execution — Microsoft (intent declaration roadmap + AGT)](https://developer.microsoft.com/blog/securing-mcp-a-control-plane-for-agent-tool-execution)
- [Governing MCP tool calls with the Agent Governance Toolkit — .NET Blog](https://devblogs.microsoft.com/dotnet/governing-mcp-tool-calls-in-dotnet-with-the-agent-governance-toolkit/)
- [Secure Intent Protocol: JWT-Compatible Agentic Identity (IETF draft-goswami-agentic-jwt)](https://www.ietf.org/archive/id/draft-goswami-agentic-jwt-00.html)
- [MCP — Specification 2025-06-18 (elicitation & sampling)](https://modelcontextprotocol.io/specification/2025-06-18/client/sampling)
- [Beyond request-response: how MCP servers are learning to collaborate — WorkOS](https://workos.com/blog/beyond-request-response-mcp)
- [Agent2Agent (A2A) Protocol Specification (signed Agent Cards, securitySchemes)](https://a2a-protocol.org/latest/specification/)
- [A2A — Enterprise-Ready Features](https://a2a-protocol.org/latest/topics/enterprise-ready/)
- [IBM — What is Agent Communication Protocol (ACP)?](https://www.ibm.com/think/topics/agent-communication-protocol)
- [Agent Network Protocol (ANP) Technical White Paper (arXiv:2508.00007)](https://arxiv.org/html/2508.00007v1)
- [Cisco Outshift — AGNTCY: building the Internet of Agents](https://outshift.cisco.com/blog/building-internet-of-agents-interoperable-framework)
- [What is the Agent Gateway Protocol (AGP)? (BGP-inspired, Intent payloads)](https://www.theainavigator.com/blog/what-is-the-agent-gateway-protocol-agp)
- [Solo.io — Agentic AI: Runtime Guardrails and Policy Enforcement](https://www.solo.io/blog/agentic-ai-runtime-guardrails)
- [OpenTelemetry — AI Agent Observability: Evolving Standards](https://opentelemetry.io/blog/2025/ai-agent-observability/)
- [AI Agents with Decentralized Identifiers and Verifiable Credentials (arXiv:2511.02841)](https://arxiv.org/html/2511.02841v1)
- [Microsoft — Authorization and Governance for AI Agents: Runtime Authorization Beyond Identity](https://techcommunity.microsoft.com/blog/microsoft-security-blog/authorization-and-governance-for-ai-agents-runtime-authorization-beyond-identity/4509161)
