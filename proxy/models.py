"""VAP data models.

These mirror the four JSON Schemas in ``schemas/`` (the authoritative contract).
We deliberately avoid a hard dependency on pydantic so the in-process / subprocess
test path runs on a bare stdlib Python (the sandbox has no PyPI access). These are
plain dataclasses with light validation; ``schemas/`` remains the source of truth.

Key contract facts encoded here (from the schemas):
* every message carries ``vap`` (== "0.1"), ``type`` and ``session_id`` at top level
* intent.sensitivity enum: reads|writes_data|writes_money|deletes|sends_external|grants_access
* scope gates on the PUBLIC tool namespace only (tools_allow / tools_deny patterns);
  it carries NO argument-level semantics -- those live in deployment-local operator
  policy (the C4 policy hook), not in the wire schema
* verification.checks are STRINGS; method in
  static|static+policy|static+semantic|static+policy+semantic
* semantic_trigger enum: session_start|amendment|sensitivity|burn_rate_anomaly|
  looping|novelty|threshold_proximity|random_sample|scope_boundary
* budget is unit-agnostic: universal max_calls/deadline + a `limits` map of named
  meters (tokens, usd_opcost, <custom>) -> max value; tools self-declare opaque
  per-call meter contributions and the proxy only sums/enforces ceilings
* amendment uses add_scope / increase_budget (add_calls / extend_deadline /
  add_limits map) / prev_commitment_digest / reason
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VAP_VERSION = "0.1"

SENSITIVITY_ENUM = {
    "reads", "writes_data", "writes_money", "deletes", "sends_external", "grants_access",
}


class VapError(Exception):
    """Malformed VAP payload (maps to a JSON-RPC error / denied verdict)."""


# --------------------------------------------------------------------------- #
# Scope Commitment (session init)
# --------------------------------------------------------------------------- #
@dataclass
class Principal:
    agent_id: str
    auth: Optional[str] = None
    delegated_by: Optional[str] = None

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Principal":
        if not isinstance(d, dict) or not d.get("agent_id"):
            raise VapError("principal.agent_id is required")
        return Principal(agent_id=d["agent_id"], auth=d.get("auth"),
                         delegated_by=d.get("delegated_by"))

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            "agent_id": self.agent_id, "auth": self.auth,
            "delegated_by": self.delegated_by}.items() if v is not None}


@dataclass
class Scope:
    """Tool-name envelope. Gates on the PUBLIC tool namespace only (the names a
    server advertises via MCP tools/list); never on argument values."""
    tools_allow: List[str] = field(default_factory=list)
    tools_deny: List[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Scope":
        d = d or {}
        if not d.get("tools_allow"):
            raise VapError("scope.tools_allow is required and must be non-empty")
        return Scope(
            tools_allow=list(d.get("tools_allow") or []),
            tools_deny=list(d.get("tools_deny") or []),
        )


@dataclass
class Budget:
    """Multi-dimensional, unit-agnostic budget.

    ``max_calls`` and ``deadline`` are universal first-class meters. ``limits`` is an
    extensible map of named meters -> max value (e.g. {"tokens": 100000,
    "usd_opcost": 5, "disbursed_usd": 1000, "<custom>": ...}). A USD operational cap
    is just ``limits["usd_opcost"]``. Tools self-declare opaque per-call contributions
    to these meters; the proxy tracks per-meter consumption against ``limits`` and
    denies when ANY meter (or calls/deadline) would be exceeded -- without knowing
    what any meter means.
    """
    max_calls: Optional[int] = None
    deadline: Optional[str] = None
    limits: Dict[str, float] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Budget":
        d = d or {}
        limits = dict(d.get("limits") or {})
        # Back-compat shim: a legacy top-level max_cost_usd maps to limits["usd_opcost"].
        if d.get("max_cost_usd") is not None and "usd_opcost" not in limits:
            limits["usd_opcost"] = d["max_cost_usd"]
        return Budget(max_calls=d.get("max_calls"), deadline=d.get("deadline"),
                      limits=limits)


@dataclass
class ScopeCommitment:
    session_id: str
    goal: str
    scope: Scope
    principal: Principal
    budget: Budget = field(default_factory=Budget)
    plan_digest: Optional[str] = None
    signature: Optional[str] = None
    vap: str = VAP_VERSION

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ScopeCommitment":
        if not isinstance(d, dict):
            raise VapError("scope_commitment must be an object")
        if not d.get("goal"):
            raise VapError("goal is required")
        if not d.get("session_id"):
            raise VapError("session_id is required")
        return ScopeCommitment(
            session_id=d["session_id"],
            goal=d["goal"],
            scope=Scope.from_dict(d.get("scope", {})),
            principal=Principal.from_dict(d.get("principal", {})),
            budget=Budget.from_dict(d.get("budget")),
            plan_digest=d.get("plan_digest"),
            signature=d.get("signature"),
            vap=d.get("vap", VAP_VERSION),
        )


# --------------------------------------------------------------------------- #
# Intent Envelope (per call)
# --------------------------------------------------------------------------- #
@dataclass
class Intent:
    rationale: str
    expected_effect: str
    step: Optional[int] = None
    sensitivity: Optional[str] = None
    reasoning_digest: Optional[str] = None

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Intent":
        if not isinstance(d, dict) or not d.get("rationale"):
            raise VapError("intent.rationale is required")
        if not d.get("expected_effect"):
            raise VapError("intent.expected_effect is required")
        sens = d.get("sensitivity")
        if sens is not None and sens not in SENSITIVITY_ENUM:
            raise VapError(f"intent.sensitivity must be one of {sorted(SENSITIVITY_ENUM)}")
        return Intent(rationale=d["rationale"], expected_effect=d["expected_effect"],
                      step=d.get("step"), sensitivity=sens,
                      reasoning_digest=d.get("reasoning_digest"))

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            "rationale": self.rationale, "expected_effect": self.expected_effect,
            "step": self.step, "sensitivity": self.sensitivity,
            "reasoning_digest": self.reasoning_digest}.items() if v is not None}


@dataclass
class Call:
    tool: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Call":
        if not isinstance(d, dict) or not d.get("tool"):
            raise VapError("call.tool is required")
        return Call(tool=d["tool"], arguments=dict(d.get("arguments") or {}))

    def to_dict(self) -> Dict[str, Any]:
        return {"tool": self.tool, "arguments": self.arguments}


@dataclass
class IntentEnvelope:
    session_id: str
    intent: Intent
    call: Call
    signature: Optional[str] = None
    vap: str = VAP_VERSION

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "IntentEnvelope":
        if not isinstance(d, dict):
            raise VapError("intent envelope must be an object")
        return IntentEnvelope(
            session_id=d.get("session_id", ""),
            intent=Intent.from_dict(d.get("intent", {})),
            call=Call.from_dict(d.get("call", {})),
            signature=d.get("signature"),
            vap=d.get("vap", VAP_VERSION),
        )


# --------------------------------------------------------------------------- #
# Scope Amendment
# --------------------------------------------------------------------------- #
@dataclass
class ScopeAmendment:
    session_id: str
    prev_commitment_digest: str
    reason: str
    add_scope: Optional[Dict[str, Any]] = None
    increase_budget: Optional[Dict[str, Any]] = None
    new_plan_digest: Optional[str] = None
    signature: Optional[str] = None
    vap: str = VAP_VERSION

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ScopeAmendment":
        if not isinstance(d, dict) or not d.get("session_id"):
            raise VapError("amendment.session_id is required")
        if not d.get("reason"):
            raise VapError("amendment.reason is required")
        if not d.get("add_scope") and not d.get("increase_budget"):
            raise VapError("amendment must include add_scope and/or increase_budget")
        return ScopeAmendment(
            session_id=d["session_id"],
            prev_commitment_digest=d.get("prev_commitment_digest", ""),
            reason=d["reason"],
            add_scope=d.get("add_scope"),
            increase_budget=d.get("increase_budget"),
            new_plan_digest=d.get("new_plan_digest"),
            signature=d.get("signature"),
            vap=d.get("vap", VAP_VERSION),
        )


# --------------------------------------------------------------------------- #
# Verdict (response)
# --------------------------------------------------------------------------- #
SERVED, CLARIFY, DOWNGRADED, DENIED = "served", "clarify", "downgraded", "denied"

# verification.method enum
M_STATIC = "static"
M_STATIC_POLICY = "static+policy"
M_STATIC_SEMANTIC = "static+semantic"
M_STATIC_POLICY_SEMANTIC = "static+policy+semantic"


@dataclass
class Verification:
    checks: List[str] = field(default_factory=list)   # schema: array of strings
    method: str = M_STATIC
    risk_score: Optional[float] = None
    semantic_invoked: bool = False
    semantic_trigger: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"checks": self.checks, "method": self.method,
                               "semantic_invoked": self.semantic_invoked}
        if self.risk_score is not None:
            out["risk_score"] = round(self.risk_score, 4)
        if self.semantic_trigger is not None:
            out["semantic_trigger"] = self.semantic_trigger
        if self.confidence is not None:
            out["confidence"] = round(self.confidence, 4)
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass
class Clarification:
    question: str
    schema: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {"question": self.question}
        if self.schema:
            out["schema"] = self.schema
        return out


@dataclass
class Verdict:
    session_id: str
    verdict: str
    verification: Verification
    in_response_to: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    accepted_commitment_digest: Optional[str] = None
    clarification: Optional[Clarification] = None
    audit_ref: Optional[str] = None
    signature: Optional[str] = None
    vap: str = VAP_VERSION
    type: str = "verdict"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "vap": self.vap, "type": "verdict", "session_id": self.session_id,
            "verdict": self.verdict, "verification": self.verification.to_dict(),
        }
        if self.in_response_to:
            out["in_response_to"] = self.in_response_to
        if self.result is not None:
            out["result"] = self.result
        if self.accepted_commitment_digest:
            out["accepted_commitment_digest"] = self.accepted_commitment_digest
        if self.clarification:
            out["clarification"] = self.clarification.to_dict()
        if self.audit_ref:
            out["audit_ref"] = self.audit_ref
        if self.signature:
            out["signature"] = self.signature
        return out
