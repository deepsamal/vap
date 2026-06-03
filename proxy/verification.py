"""Core VAP verification engine.

Two-tier flow:

SESSION START (initialize w/ _meta.vap.scope_commitment)
    S1 authn/authz  (mock: accept if principal.agent_id present)
    S2 SEMANTIC     goal coherent + scope proportionate -> cache commitment

PER CALL (tools/call w/ _meta.vap.intent)
    C1 BIND     session has an accepted commitment
    C2 SCOPE    tool name in tools_allow, not in tools_deny (globs). PUBLIC tool
                namespace ONLY -- no argument inspection lives in this core path.
    C3 BUDGET   max_calls / deadline / per-named-meter limits (tokens, usd_opcost,
                disbursed_usd, ...). Tools self-declare opaque per-call meter
                contributions (result _meta.vap.cost); the proxy only sums/enforces.
    C4 POLICY   OPTIONAL deployment-local operator policy hook (OPA/Cedar/Rego-style).
                Tool-specific, argument-level rules live HERE -- loaded from
                vap-gateway.yaml (policy.rules), NOT part of the standardized wire
                schema. This is where e.g. "payments.refund requires amount<=200 and a
                ticket id" lives, as LOCAL POLICY rather than a protocol field.
    C5 SEMANTIC gated by risk scorer; judge consistency vs goal+rationale

AMENDMENT (vap/amend) -- re-run S2, re-baseline commitment, audit it.

Transport-agnostic: takes/returns plain dicts + Verdict objects, so it works behind
FastAPI or stdlib http.server identically.
"""

from __future__ import annotations

import fnmatch
import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from audit import AuditLog
from models import (
    CLARIFY, DENIED, DOWNGRADED, SERVED,
    M_STATIC, M_STATIC_POLICY, M_STATIC_SEMANTIC, M_STATIC_POLICY_SEMANTIC,
    Call, Clarification, Intent, IntentEnvelope, ScopeAmendment, ScopeCommitment,
    VapError, Verdict, Verification,
)
from risk_scorer import RiskConfig, RiskScorer
from semantic_judge import SemanticJudge, get_judge

# tool -> effect class (drives policy C4 + risk scorer)
DEFAULT_TOOL_META = {
    "crm.read": "read",
    "payments.refund": "writes_money",
    "tickets.update": "writes_data",
    "tickets.escalate": "writes_data",
    "admin.delete_user": "deletes",
}

# Cost model (FALLBACK only). The authoritative per-call cost is whatever the TOOL
# self-declares in its MCP result _meta.vap.cost; VAP sums those opaque contributions.
# "calls" is always implicitly 1 (added in _call_cost). The point of a unit-agnostic
# budget: payments.refund burns ~no tokens but is exactly the call you most want to
# cap -- so it carries an operational processing cost AND can self-report a business
# `disbursed_usd` contribution that a cumulative meter bounds in aggregate.
# These are OPERATOR-CONFIGURED default per-call meter contributions used ONLY as a
# fallback when a tool does not self-declare its cost in the MCP result _meta.vap.cost.
# Meter names are opaque to VAP: `usd_opcost` = operational processing cost (named so
# it never collides with a business amount), `disbursed_usd` = a representative
# default for the refund tool's self-reported business disbursement (the real value
# comes back per-call in _meta.vap.cost).
DEFAULT_TOOL_COSTS = {
    "crm.read":          {"tokens": 200},
    "payments.refund":   {"usd_opcost": 0.01, "tokens": 50, "disbursed_usd": 0.0},
    "tickets.update":    {"tokens": 120},
    "tickets.escalate":  {"tokens": 120},
    "admin.delete_user": {"usd_opcost": 0.0, "tokens": 80},
}
# Fallback cost for tools not in the map.
DEFAULT_FALLBACK_COST = {"tokens": 100}


# --------------------------------------------------------------------------- #
# C4 DEPLOYMENT-LOCAL POLICY HOOK  (NOT part of the VAP wire protocol)
# --------------------------------------------------------------------------- #
# Operator-authored, tool-specific rules (OPA/Cedar/Rego-style) live here. They are
# loaded from vap-gateway.yaml (policy.rules) -- pure deployment-local config. This is
# the ONE place argument-level semantics are allowed: VAP itself stays tool-agnostic.
#
# A rule matches on the public tool name (glob) and asserts simple conditions on the
# call arguments and/or the intent rationale. The built-in engine below is sufficient
# for the demo; a production deployment would delegate to OPA/Cedar instead.
#
# Rule shape (from YAML):
#   - name: refund_cap
#     tool: payments.refund          # glob over public tool name
#     deny_unless:                    # ALL must hold or the call is DENIED
#       arg_max: { amount_usd: 200 } # arguments.amount_usd <= 200
#       arg_min: { ... }             # arguments.<k> >= v
#       rationale_matches: "ticket|tck[-_ ]?\\d+|case[-_ ]?\\d+|#\\d+"
#     message: "refund over $200 or missing ticket id"

class PolicyRule:
    """One operator-local argument-level rule. Deployment config, not protocol."""

    def __init__(self, spec: Dict[str, Any]):
        self.name = str(spec.get("name") or "rule")
        self.tool = str(spec.get("tool") or "*")
        du = spec.get("deny_unless") or {}
        self.arg_max = dict(du.get("arg_max") or {})
        self.arg_min = dict(du.get("arg_min") or {})
        self.rationale_re = du.get("rationale_matches")
        self.message = spec.get("message") or f"violates local policy rule '{self.name}'"

    def applies_to(self, tool: str) -> bool:
        return fnmatch.fnmatch(tool, self.tool)

    def check(self, call: "Call", intent: "Intent") -> Tuple[bool, str]:
        """Return (ok, detail). ok=True means the rule is satisfied."""
        import re
        args = call.arguments or {}
        for k, mx in self.arg_max.items():
            v = args.get(k)
            if isinstance(v, (int, float)) and isinstance(mx, (int, float)) and v > mx:
                return False, f"{self.name}: arguments.{k}={v} exceeds max {mx}"
        for k, mn in self.arg_min.items():
            v = args.get(k)
            if isinstance(v, (int, float)) and isinstance(mn, (int, float)) and v < mn:
                return False, f"{self.name}: arguments.{k}={v} below min {mn}"
        if self.rationale_re:
            text = " ".join(filter(None, [intent.rationale, intent.expected_effect]))
            if not re.search(self.rationale_re, text, re.I):
                return False, f"{self.name}: rationale does not match required pattern"
        return True, f"{self.name}: satisfied"


# Built-in default policy used on the zero-dep test path (the tiny stdlib YAML parser
# cannot model nested lists/maps, so these mirror the vap-gateway.yaml policy.rules).
DEFAULT_POLICY_RULES = [
    {
        "name": "refund_cap",
        "tool": "payments.refund",
        "deny_unless": {
            "arg_max": {"amount_usd": 200},
            "rationale_matches": r"ticket|tck[-_ ]?\d+|sup[-_ ]?\d+|#\d+|case[-_ ]?\d+",
        },
        "message": "refund over $200 or missing ticket id (local policy refund_cap)",
    },
]


def _parse_deadline(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        t = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Session:
    session_id: str
    commitment: ScopeCommitment
    commitment_digest: str
    accepted: bool = False
    calls_made: int = 0
    consumed: Dict[str, float] = field(default_factory=dict)
    used_tools: set = field(default_factory=set)
    seen_calls: Dict[str, int] = field(default_factory=dict)
    judge_cache: Dict[str, Tuple[bool, float, str]] = field(default_factory=dict)
    # audit_ref -> the tool-declared cost estimate billed for that served call, so a
    # later reconcile can replace it with the tool's self-reported _meta.vap.cost.
    pending_costs: Dict[str, Dict[str, float]] = field(default_factory=dict)
    created: float = field(default_factory=time.time)


@dataclass
class EngineConfig:
    invoke_at: float = 0.5
    random_sample_rate: float = 0.0
    random_seed: int = 1337
    default_max_calls: Optional[int] = None
    default_limits: Dict[str, float] = field(default_factory=dict)
    policy_rules: List[Dict[str, Any]] = field(
        default_factory=lambda: [dict(r) for r in DEFAULT_POLICY_RULES])
    tool_meta: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TOOL_META))
    tool_costs: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_TOOL_COSTS.items()})
    fallback_cost: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FALLBACK_COST))

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "EngineConfig":
        d = d or {}
        sj, rs, bud, pol = (d.get("semantic_judge", {}) or {}, d.get("risk_scorer", {}) or {},
                            d.get("budget", {}) or {}, d.get("policy", {}) or {})
        cfg = EngineConfig()
        cfg.invoke_at = sj.get("invoke_at", cfg.invoke_at)
        cfg.random_sample_rate = rs.get("random_sample_rate", cfg.random_sample_rate)
        cfg.random_seed = rs.get("seed", cfg.random_seed)
        cfg.default_max_calls = bud.get("default_max_calls", cfg.default_max_calls)
        if isinstance(bud.get("default_limits"), dict):
            cfg.default_limits = dict(bud["default_limits"])
        # C4 deployment-local policy rules (operator config, not protocol). When the
        # YAML provides a rules list we use it; otherwise the built-in defaults apply
        # (the tiny stdlib YAML parser cannot model the nested list, so the zero-dep
        # test path relies on DEFAULT_POLICY_RULES).
        rules = pol.get("rules")
        if isinstance(rules, list) and rules:
            cfg.policy_rules = [dict(r) for r in rules if isinstance(r, dict)]
        if d.get("tool_meta"):
            cfg.tool_meta.update(d["tool_meta"])
        if isinstance(d.get("tool_costs"), dict):
            for tool, cost in d["tool_costs"].items():
                if isinstance(cost, dict):
                    cfg.tool_costs[tool] = dict(cost)
        if isinstance(d.get("fallback_cost"), dict):
            cfg.fallback_cost = dict(d["fallback_cost"])
        return cfg


# convenience: high-tier sensitivities that force the judge regardless of score
_HIGH_SENS = {"writes_money", "deletes", "grants_access"}


class VerificationEngine:
    def __init__(self, config=None, audit=None, judge=None):
        self.cfg = config or EngineConfig()
        self.audit = audit or AuditLog()
        self.judge = judge or get_judge()
        self.scorer = RiskScorer(
            RiskConfig(random_sample_rate=self.cfg.random_sample_rate,
                       seed=self.cfg.random_seed),
            tool_meta=self.cfg.tool_meta)
        # Compile the deployment-local C4 policy rules (operator config, not protocol).
        self.policy_rules = [PolicyRule(r) for r in self.cfg.policy_rules]
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # SESSION START : S1 + S2
    # ------------------------------------------------------------------ #
    def open_session(self, session_id: str, raw_commitment: Dict[str, Any]) -> Verdict:
        checks: List[str] = []
        raw_commitment = dict(raw_commitment or {})
        raw_commitment.setdefault("session_id", session_id)
        try:
            c = ScopeCommitment.from_dict(raw_commitment)
        except VapError as e:
            return self._session_verdict(session_id, None, None, DENIED,
                                         [f"schema_invalid: {e}"], M_STATIC,
                                         reason=f"malformed commitment: {e}")
        if c.budget.max_calls is None:
            c.budget.max_calls = self.cfg.default_max_calls
        for meter, mx in self.cfg.default_limits.items():
            c.budget.limits.setdefault(meter, mx)
        dg = c.plan_digest or digest(c.goal)

        # S1
        s1 = bool(c.principal.agent_id)
        checks.append("S1_authn_ok" if s1 else "S1_authn_fail")
        if not s1:
            return self._session_verdict(session_id, c, dg, DENIED, checks, M_STATIC,
                                         reason="authentication failed (no agent_id)")
        # S2 semantic
        consistent, conf, reason = self._judge_scope(c)
        checks.append("S2_semantic_ok" if consistent else "S2_semantic_fail")
        verdict = SERVED if consistent else CLARIFY
        if consistent:
            with self._lock:
                self.sessions[session_id] = Session(
                    session_id=session_id, commitment=c, commitment_digest=dg,
                    accepted=True)
        clar = None if consistent else Clarification(
            question="The requested scope does not look proportionate to the stated "
                     "goal. Please narrow scope or justify.")
        return self._session_verdict(
            session_id, c, dg, verdict, checks, M_STATIC_SEMANTIC,
            semantic_invoked=True, semantic_trigger="session_start",
            confidence=conf, reason=reason, accepted_digest=dg if consistent else None,
            clarification=clar)

    def _judge_scope(self, c: ScopeCommitment) -> Tuple[bool, float, str]:
        if len(c.goal.strip()) < 3:
            return (False, 0.95, "goal is empty or incoherent")
        wants_delete = any(fnmatch.fnmatch("admin.delete_user", p) for p in c.scope.tools_allow)
        goal_l = c.goal.lower()
        mentions = any(w in goal_l for w in
                       ("delete", "offboard", "remov", "terminat", "erasure", "gdpr"))
        if wants_delete and not mentions:
            return (False, 0.9,
                    "scope requests admin.delete_user but goal does not justify "
                    "destructive access (disproportionate)")
        return (True, 0.85, "goal coherent; requested scope proportionate to goal")

    def _session_verdict(self, session_id, c, dg, verdict, checks, method, *,
                         semantic_invoked=False, semantic_trigger=None, confidence=None,
                         reason=None, accepted_digest=None, clarification=None) -> Verdict:
        ver = Verification(checks=checks, method=method, semantic_invoked=semantic_invoked,
                           semantic_trigger=semantic_trigger, confidence=confidence,
                           reason=reason)
        ref = self.audit.record(
            session_id=session_id, kind="session_init", commitment_digest=dg,
            principal=(c.principal.to_dict() if c else None), intent=None, call=None,
            verdict=verdict, verification=ver.to_dict(),
            extra={"goal": c.goal if c else None})
        return Verdict(session_id=session_id, verdict=verdict, verification=ver,
                       in_response_to="scope_commitment", audit_ref=ref,
                       accepted_commitment_digest=accepted_digest,
                       clarification=clarification, signature="hmac:proxy")

    # ------------------------------------------------------------------ #
    # PER CALL : C1..C5
    # ------------------------------------------------------------------ #
    def verify_call(self, session_id: str, raw_intent: Dict[str, Any]
                    ) -> Tuple[Verdict, bool, Optional[Call]]:
        checks: List[str] = []
        try:
            env = IntentEnvelope.from_dict(raw_intent)
        except VapError as e:
            v = self._deny(session_id, None, None, None,
                           [f"schema_invalid: {e}"], f"malformed intent envelope: {e}")
            return v, False, None
        intent, call = env.intent, env.call

        # C1 BIND
        sess = self.sessions.get(session_id)
        c1 = sess is not None and sess.accepted
        checks.append("C1_bind_ok" if c1 else "C1_bind_fail")
        if not c1:
            v = self._deny(session_id, None, intent, call, checks,
                           "no accepted scope commitment (unbound call) -- drift")
            return v, False, call
        c = sess.commitment

        # C2 SCOPE
        c2_ok, c2_detail = self._check_scope(call, c)
        checks.append("C2_scope_ok" if c2_ok else "C2_scope_fail")
        if not c2_ok:
            v = self._deny(session_id, sess, intent, call, checks,
                           "scope violation / drift: " + c2_detail)
            return v, False, call

        # C3 BUDGET (multi-meter, unit-agnostic). Compute this call's cost first so we
        # can also bill live judge token usage into it before final accounting. This is
        # the OPERATOR FALLBACK estimate (tool_costs); the tool's authoritative
        # self-reported contribution (_meta.vap.cost) reconciles it post-execution.
        cost = self._call_cost(call.tool)
        tool_estimate = {m: a for m, a in cost.items() if m != "calls"}
        c3_ok, c3_detail = self._check_budget(sess, c, cost)
        checks.append("C3_budget_ok" if c3_ok else "C3_budget_fail")
        if not c3_ok:
            v = self._deny(session_id, sess, intent, call, checks,
                           "budget exceeded: " + c3_detail)
            return v, False, call

        # C4 POLICY (deployment-local operator hook; +policy in the method label).
        c4_ok, c4_rule, c4_detail = self._check_policy(call, intent)
        checks.append("C4_policy_ok" if c4_ok else "C4_policy_fail")
        if c4_rule:
            # Attribute the decision to the LOCAL POLICY rule by name so denials are
            # traceable to operator config, not to any (removed) protocol field.
            checks.append(("policy_rule_ok:" if c4_ok else "policy_rule_fail:") + c4_rule)
        if not c4_ok:
            v = self._deny(session_id, sess, intent, call, checks,
                           "local policy violation: " + c4_detail, method=M_STATIC_POLICY)
            return v, False, call

        # ---- deterministic gate passed; score risk, maybe fire judge (C5) ----
        risk = self.scorer.score(
            call.tool, call.arguments, intent.sensitivity,
            seen_calls=sess.seen_calls, used_tools=sess.used_tools,
            calls_made=sess.calls_made,
            cost_spent=sess.consumed.get("usd_opcost", 0.0),
            max_cost=c.budget.limits.get("usd_opcost"), max_calls=c.budget.max_calls)

        high_sens = intent.sensitivity in _HIGH_SENS
        fire = (risk.score >= self.cfg.invoke_at) or high_sens
        method = M_STATIC_POLICY
        semantic_invoked = False
        semantic_trigger = None
        confidence = None
        reason = "deterministic checks passed; risk below threshold"

        if fire:
            semantic_invoked = True
            method = M_STATIC_POLICY_SEMANTIC
            if high_sens and risk.score < self.cfg.invoke_at:
                semantic_trigger = "sensitivity"
            else:
                semantic_trigger = risk.trigger or "sensitivity"
            cache_key = "|".join([c.plan_digest or sess.commitment_digest, call.tool,
                                  self.scorer.call_key(call.tool, call.arguments)])
            if cache_key in sess.judge_cache:
                consistent, confidence, jreason, jtokens = sess.judge_cache[cache_key]
                jreason = "(cached) " + jreason
                jtokens = 0  # cached -> no fresh verification spend
            else:
                consistent, confidence, jreason, jtokens = self.judge.evaluate(
                    c.goal, intent.rationale, intent.expected_effect, call.to_dict())
                sess.judge_cache[cache_key] = (consistent, confidence, jreason, jtokens)
            # Verification cost is itself budgeted: bill the judge's token usage to the
            # session's `tokens` meter (only matters for the real LLM judge; mock = 0).
            if jtokens:
                cost["tokens"] = float(cost.get("tokens", 0.0)) + float(jtokens)
                checks.append(f"C5_judge_tokens={int(jtokens)}")
                # Re-check budget now that verification tokens are added.
                c3b_ok, c3b_detail = self._check_budget(sess, c, cost)
                if not c3b_ok:
                    checks.append("C3_budget_fail")
                    v = self._deny(session_id, sess, intent, call, checks,
                                   "budget exceeded (incl. judge tokens): " + c3b_detail,
                                   method=method)
                    return v, False, call
            checks.append("C5_intent_consistent" if consistent else "C5_intent_inconsistent")
            reason = jreason
            if not consistent:
                verdict = DENIED if confidence >= 0.85 else CLARIFY
                clar = None
                if verdict == CLARIFY:
                    clar = Clarification(
                        question="This call is in-scope but does not look consistent "
                                 "with the session goal/rationale. Confirm intent?")
                v = self._verdict(session_id, sess, intent, call, verdict, checks, method,
                                  risk_score=risk.score, semantic_invoked=True,
                                  semantic_trigger=semantic_trigger, confidence=confidence,
                                  reason=reason, clarification=clar)
                return v, False, call
        else:
            checks.append("C5_skipped_below_threshold")

        # served -> account (per named meter) + forward
        sess.calls_made += int(cost.get("calls", 1))
        for meter, amt in cost.items():
            if meter == "calls":
                continue
            sess.consumed[meter] = sess.consumed.get(meter, 0.0) + float(amt)
        sess.used_tools.add(call.tool)
        k = self.scorer.call_key(call.tool, call.arguments)
        sess.seen_calls[k] = sess.seen_calls.get(k, 0) + 1

        v = self._verdict(session_id, sess, intent, call, SERVED, checks, method,
                          risk_score=risk.score, semantic_invoked=semantic_invoked,
                          semantic_trigger=semantic_trigger, confidence=confidence,
                          reason=reason)
        # Remember the tool-declared estimate so reconcile_cost() can replace it with
        # the tool's self-reported contribution once the upstream result is in.
        if v.audit_ref:
            sess.pending_costs[v.audit_ref] = dict(tool_estimate)
        return v, True, call

    # ------------------------------------------------------------------ #
    def _check_scope(self, call: Call, c: ScopeCommitment) -> Tuple[bool, str]:
        # C2 gates on the PUBLIC tool namespace ONLY (tool name vs tools_allow \\
        # tools_deny, glob supported). No argument inspection in the core path --
        # argument-level rules are deployment-local operator policy (C4).
        tool = call.tool
        if any(fnmatch.fnmatch(tool, p) for p in c.scope.tools_deny):
            return False, f"tool '{tool}' matches tools_deny"
        if not any(fnmatch.fnmatch(tool, p) for p in c.scope.tools_allow):
            return False, f"tool '{tool}' not in tools_allow {c.scope.tools_allow}"
        return True, f"tool '{tool}' in public tool namespace (tools_allow)"

    def _call_cost(self, tool: str) -> Dict[str, float]:
        """Per-call consumption dict, e.g. {"calls":1, "tokens":200, "usd_opcost":0.01}.

        "calls" is always 1; the rest comes from the tool cost model. Returned as a
        fresh dict the caller may mutate (e.g. to add live judge token usage)."""
        cost = dict(self.cfg.tool_costs.get(tool, self.cfg.fallback_cost))
        cost["calls"] = 1
        return cost

    def _check_budget(self, sess: Session, c: ScopeCommitment,
                      cost: Dict[str, float]) -> Tuple[bool, str]:
        """Multi-meter, unit-agnostic budget check (C3).

        Denies if ANY of: projected calls > max_calls, deadline passed, or projected
        consumption of ANY named meter > its limit. The reason names the tripped meter.
        """
        b = c.budget
        # universal: calls
        proj_calls = sess.calls_made + int(cost.get("calls", 1))
        if b.max_calls is not None and proj_calls > b.max_calls:
            return False, f"meter 'calls' would be {proj_calls} > max_calls {b.max_calls}"
        # universal: deadline
        if b.deadline:
            dl = _parse_deadline(b.deadline)
            if dl is not None and time.time() > dl:
                return False, f"meter 'deadline' tripped: past deadline {b.deadline}"
        # named meters in limits
        parts = [f"calls {proj_calls}/{b.max_calls}"]
        for meter, limit in b.limits.items():
            spent = sess.consumed.get(meter, 0.0)
            proj = spent + float(cost.get(meter, 0.0))
            if proj > float(limit) + 1e-9:
                return False, (f"meter '{meter}' would be {proj:g} > limit {limit:g}")
            parts.append(f"{meter} {proj:g}/{limit:g}")
        return True, "within budget: " + ", ".join(parts)

    def _check_policy(self, call: Call, intent: Intent) -> Tuple[bool, Optional[str], str]:
        """C4 OPTIONAL deployment-local operator policy hook.

        Evaluates the operator-authored, argument-level rules (loaded from
        vap-gateway.yaml policy.rules). This is the ONLY place argument semantics are
        considered; it is deployment-local config, NOT the VAP wire protocol. Returns
        (ok, rule_name, detail) -- rule_name names the matched/violated policy rule so
        the verdict can surface it (and so denials are attributable to a LOCAL POLICY
        rule rather than a protocol field).
        """
        matched: List[str] = []
        for rule in self.policy_rules:
            if not rule.applies_to(call.tool):
                continue
            ok, detail = rule.check(call, intent)
            matched.append(rule.name)
            if not ok:
                return False, rule.name, rule.message + " :: " + detail
        if matched:
            return True, matched[-1], "local policy rules satisfied: " + ", ".join(matched)
        return True, None, "no local policy rule applies to this tool"

    # ------------------------------------------------------------------ #
    # AMENDMENT
    # ------------------------------------------------------------------ #
    def amend(self, raw: Dict[str, Any]) -> Verdict:
        checks: List[str] = []
        try:
            am = ScopeAmendment.from_dict(raw)
        except VapError as e:
            ver = Verification(checks=[f"schema_invalid: {e}"], method=M_STATIC,
                               reason="malformed amendment")
            return Verdict(session_id=raw.get("session_id", ""), verdict=DENIED,
                           verification=ver, in_response_to="scope_amendment",
                           signature="hmac:proxy")
        sess = self.sessions.get(am.session_id)
        if not sess:
            ver = Verification(checks=["amend_bind_fail"], method=M_STATIC,
                               reason="no such session")
            ref = self.audit.record(session_id=am.session_id, kind="amendment",
                                    commitment_digest=None, principal=None, intent=None,
                                    call=None, verdict=DENIED, verification=ver.to_dict())
            return Verdict(session_id=am.session_id, verdict=DENIED, verification=ver,
                           in_response_to="scope_amendment", audit_ref=ref,
                           signature="hmac:proxy")

        # legit re-baseline requires a signature (the spec: signed re-baselining)
        sig_ok = bool(am.signature)
        checks.append("amend_signed_ok" if sig_ok else "amend_signed_fail")
        if not sig_ok:
            ver = Verification(checks=checks, method=M_STATIC,
                               reason="amendments must be signed to re-baseline scope")
            ref = self.audit.record(session_id=am.session_id, kind="amendment",
                                    commitment_digest=sess.commitment_digest,
                                    principal=sess.commitment.principal.to_dict(),
                                    intent=None, call=None, verdict=DENIED,
                                    verification=ver.to_dict(), extra={"reason": am.reason})
            return Verdict(session_id=am.session_id, verdict=DENIED, verification=ver,
                           in_response_to="scope_amendment", audit_ref=ref,
                           signature="hmac:proxy")

        # apply widening (add_scope widens, increase_budget increases)
        c = sess.commitment
        if am.add_scope:
            a = am.add_scope
            if a.get("tools_allow"):
                for t in a["tools_allow"]:
                    if t not in c.scope.tools_allow:
                        c.scope.tools_allow.append(t)
            if a.get("tools_deny"):
                for t in a["tools_deny"]:
                    if t not in c.scope.tools_deny:
                        c.scope.tools_deny.append(t)
            # add_scope widens the PUBLIC tool namespace only (tools_allow/tools_deny);
            # it carries no argument-level semantics (those are deployment-local policy).
        if am.increase_budget:
            ib = am.increase_budget
            if "add_calls" in ib and c.budget.max_calls is not None:
                c.budget.max_calls += ib["add_calls"]
            if "extend_deadline" in ib:
                c.budget.deadline = ib["extend_deadline"]
            # generic, unit-agnostic: raise each named meter's ceiling in limits.
            for meter, amt in (ib.get("add_limits") or {}).items():
                c.budget.limits[meter] = c.budget.limits.get(meter, 0.0) + float(amt)

        # re-run S2 (deliberate, audited re-invocation of the costly check)
        consistent, conf, reason = self._judge_scope(c)
        checks.append("amend_S2_ok" if consistent else "amend_S2_fail")
        verdict = SERVED if consistent else CLARIFY
        if consistent:
            sess.commitment_digest = digest(
                c.goal + str(c.scope.tools_allow) + str(c.scope.tools_deny)
                + str(sorted(c.budget.limits.items())) + str(c.budget.max_calls)
                + str(c.budget.deadline))
            sess.judge_cache.clear()
        ver = Verification(checks=checks, method=M_STATIC_SEMANTIC, semantic_invoked=True,
                           semantic_trigger="amendment", confidence=conf, reason=reason)
        ref = self.audit.record(session_id=am.session_id, kind="amendment",
                                commitment_digest=sess.commitment_digest,
                                principal=sess.commitment.principal.to_dict(),
                                intent=None,
                                call={"add_scope": am.add_scope,
                                      "increase_budget": am.increase_budget},
                                verdict=verdict, verification=ver.to_dict(),
                                extra={"reason": am.reason})
        return Verdict(session_id=am.session_id, verdict=verdict, verification=ver,
                       in_response_to="scope_amendment", audit_ref=ref,
                       accepted_commitment_digest=sess.commitment_digest if consistent else None,
                       signature="hmac:proxy")

    # ------------------------------------------------------------------ #
    def record_result(self, session_id: str, audit_ref: str, result: Any) -> None:
        self.audit.record(session_id=session_id, kind="result", commitment_digest=None,
                          principal=None, intent=None, call=None, verdict="served",
                          verification={"checks": ["result_bound"], "method": "static",
                                        "semantic_invoked": False},
                          result=result, extra={"for_audit_ref": audit_ref})

    # ------------------------------------------------------------------ #
    def reconcile_cost(self, session_id: str, audit_ref: str,
                       reported_cost: Dict[str, Any]) -> Dict[str, float]:
        """CHANGE C: bill the TOOL's self-reported per-call meter contributions.

        A tool self-declares an opaque cost map in its MCP result _meta.vap.cost, e.g.
        {"tokens": 50, "usd_opcost": 0.01, "disbursed_usd": 120}. The proxy treats
        meter names as opaque -- it just sums and enforces ceilings. Here we REPLACE the
        operator-fallback estimate that was billed pre-execution with the tool's
        authoritative values for whatever meters it reports (meters it omits keep the
        fallback estimate). Returns the net consumed deltas for transparency.

        VAP holds NO tool->meter knowledge: the SEMANTICS live entirely in the tool /
        operator. This is what lets a commitment bound an aggregate side-effect (e.g.
        cumulative `disbursed_usd`) without the protocol knowing what a refund is.
        """
        sess = self.sessions.get(session_id)
        if not sess or not isinstance(reported_cost, dict):
            return {}
        estimate = sess.pending_costs.pop(audit_ref, {})
        deltas: Dict[str, float] = {}
        for meter, amt in reported_cost.items():
            if meter == "calls":
                continue
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                continue
            prior = float(estimate.get(meter, 0.0))
            delta = amt - prior
            sess.consumed[meter] = sess.consumed.get(meter, 0.0) + delta
            deltas[meter] = delta
        return deltas

    # ------------------------------------------------------------------ #
    def _deny(self, session_id, sess, intent, call, checks, reason, method=M_STATIC) -> Verdict:
        return self._verdict(session_id, sess, intent, call, DENIED, checks, method,
                             reason=reason)

    def _verdict(self, session_id, sess, intent, call, verdict, checks, method, *,
                 risk_score=None, semantic_invoked=False, semantic_trigger=None,
                 confidence=None, reason=None, clarification=None) -> Verdict:
        ver = Verification(checks=checks, method=method, risk_score=risk_score,
                           semantic_invoked=semantic_invoked,
                           semantic_trigger=semantic_trigger, confidence=confidence,
                           reason=reason)
        ref = self.audit.record(
            session_id=session_id, kind="call",
            commitment_digest=(sess.commitment_digest if sess else None),
            principal=(sess.commitment.principal.to_dict() if sess else None),
            intent=(intent.to_dict() if intent else None),
            call=(call.to_dict() if call else None),
            verdict=verdict, verification=ver.to_dict())
        return Verdict(session_id=session_id, verdict=verdict, verification=ver,
                       in_response_to="intent_call", clarification=clarification,
                       audit_ref=ref, signature="hmac:proxy")
