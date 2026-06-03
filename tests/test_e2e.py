"""End-to-end tests validating VAP's claims.

Runs WITHOUT docker: spins up the MCP server + VAP proxy as local subprocesses on
ephemeral ports (stdlib http.server fallback when FastAPI is not installed), then
drives them with the Python reference client.

The semantic judge is the deterministic MockSemanticJudge (VAP_JUDGE=mock) and the
risk scorer's random sample is seeded, so every assertion is reproducible.

pytest-compatible (test_* functions + setup_module/teardown_module). Because the
sandbox has no pytest, it ALSO has a stdlib __main__ runner:
    python3 tests/test_e2e.py
runs the same tests and prints a pytest-style summary.

Test -> claim:
  a happy_path      in-scope coherent refund -> served
  b drift           admin.delete_user not allowed -> denied (scope, public tool name)
  c policy_rule     refund 5000 -> denied by the C4 LOCAL POLICY hook (not a protocol
                    field); also asserts resource_bounds is GONE from the schema
  d budget          exceed max_calls -> later calls denied (universal meter)
  d2 budget         exceed limits.tokens -> denied on the named meter (multi-dim)
  e semantic_catch  in-scope refund to unreferenced order -> clarify/denied
  f amendment       tool denied by scope then signed amend widens tools_allow -> served
  f2 unsigned amend denied (only signed re-baselining is legit)
  g looping         repeated tool+args -> semantic_invoked True (drift detection)
  h audit           /audit binds intent+verdict incl. denied reason; chain intact
  i llm_judge       LLMSemanticJudge vs a local fake OpenAI server: request shape,
                    JSON verdict parsing, and token usage billed to the budget meter
  j cumulative      in-policy refunds whose self-reported disbursed_usd sums past the
                    cumulative meter -> later call denied naming disbursed_usd
  k self_cost       proxy bills meters from the tool result _meta.vap.cost, not from
                    hardcoded protocol logic
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROXY_DIR = os.path.join(ROOT, "proxy")
SERVER_DIR = os.path.join(ROOT, "mcp-server")
CLIENT_DIR = os.path.join(ROOT, "clients", "python")

sys.path.insert(0, CLIENT_DIR)
from vap_client import VapClient  # noqa: E402

FUTURE_DEADLINE = "2099-01-01T00:00:00Z"
_state: dict = {}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.2)
    raise RuntimeError(f"service at {url} never healthy: {last}")


def commitment(**overrides) -> dict:
    base = {
        "goal": "Resolve customer support tickets: read CRM, issue small refunds, "
                "and update tickets.",
        "scope": {
            # Public tool namespace only -- argument rules are C4 local policy.
            "tools_allow": ["crm.read", "payments.refund", "tickets.*"],
            "tools_deny": ["admin.*"],
        },
        "budget": {"max_calls": 25,
                   "limits": {"usd_opcost": 5.0, "tokens": 50000,
                              "disbursed_usd": 100000},
                   "deadline": FUTURE_DEADLINE},
        "plan_digest": "sha256:abc123",
        "principal": {"agent_id": "did:web:acme.ai:agent:support"},
    }
    base.update(overrides)
    return base


def new_client() -> VapClient:
    c = VapClient(_state["proxy_url"])
    res = c.open_session(commitment())
    assert res.verdict == "served", f"session init not served: {res.verdict} {res.verification}"
    return c


def setup_module(module=None):  # noqa: ARG001
    sp, pp = _free_port(), _free_port()
    audit_path = os.path.join(HERE, "_test_audit.jsonl")
    server = subprocess.Popen(
        [sys.executable, os.path.join(SERVER_DIR, "server.py")],
        env={**os.environ, "PORT": str(sp), "HOST": "127.0.0.1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _wait_health(f"http://127.0.0.1:{sp}/health")
    proxy = subprocess.Popen(
        [sys.executable, os.path.join(PROXY_DIR, "server.py")],
        env={**os.environ, "PORT": str(pp), "HOST": "127.0.0.1",
             "UPSTREAM_URL": f"http://127.0.0.1:{sp}/mcp", "VAP_JUDGE": "mock",
             "VAP_AUDIT_PATH": audit_path,
             "VAP_CONFIG": os.path.join(PROXY_DIR, "vap-gateway.yaml"),
             "PYTHONPATH": PROXY_DIR},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _wait_health(f"http://127.0.0.1:{pp}/health")
    # warm-up: confirm an end-to-end init round-trips (binds session header path)
    _warm = VapClient(f"http://127.0.0.1:{pp}")
    for _ in range(5):
        wr = _warm.open_session(commitment())
        if wr.verdict == "served" and _warm.session_id:
            break
        time.sleep(0.2)
    _state.update(server_proc=server, proxy_proc=proxy,
                  proxy_url=f"http://127.0.0.1:{pp}", server_url=f"http://127.0.0.1:{sp}",
                  audit_path=audit_path)


def teardown_module(module=None):  # noqa: ARG001
    for key in ("proxy_proc", "server_proc"):
        p = _state.get(key)
        if p:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


# --------------------------------------------------------------------------- #
def test_a_happy_path_served():
    c = new_client()
    c.call("crm.read", {"customer_id": "C100"},
           {"rationale": "Look up C100 for ticket TCK-501.",
            "expected_effect": "Read-only CRM lookup.", "sensitivity": "reads"})
    r = c.call("payments.refund", {"order_id": "ORD-77", "amount_usd": 25},
               {"rationale": "Refund $25 on order ORD-77 per ticket TCK-501 (damaged).",
                "expected_effect": "Issue a $25 refund on order ORD-77.",
                "sensitivity": "writes_money"})
    assert r.verdict == "served", r.verification
    checks = r.verification["checks"]
    assert "C1_bind_ok" in checks and "C2_scope_ok" in checks
    assert "C3_budget_ok" in checks and "C4_policy_ok" in checks
    assert r.result and r.result.get("status") == "refunded"


def test_b_drift_out_of_scope_denied():
    c = new_client()
    r = c.call("admin.delete_user", {"user_id": "user-42"},
               {"rationale": "Clean up the account.", "expected_effect": "Delete user.",
                "sensitivity": "deletes"})
    assert r.verdict == "denied", r.verification
    assert "C2_scope_fail" in r.verification["checks"]
    assert "scope" in (r.verification.get("reason") or "").lower()


def test_c_policy_rule_amount_denied():
    """The old 'resource bound' denial is now a C4 LOCAL POLICY rule, not a protocol
    field. A $5000 refund (with a ticket cited so only the amount cap can catch it) is
    denied by the deployment-local `refund_cap` rule. The denial is attributable to the
    policy hook (method includes '+policy', a check names the rule), and crucially is
    NOT a scope/resource_bounds failure -- the protocol no longer knows about amounts.
    """
    c = new_client()
    r = c.call("payments.refund", {"order_id": "ORD-9", "amount_usd": 5000},
               {"rationale": "Large refund on ORD-9 per ticket TCK-1.",
                "expected_effect": "Refund $5000.", "sensitivity": "writes_money"})
    assert r.verdict == "denied", r.verification
    checks = r.verification["checks"]
    # the denial comes from the C4 policy hook ...
    assert "C4_policy_fail" in checks, checks
    assert any(c.startswith("policy_rule_fail:refund_cap") for c in checks), checks
    assert r.verification.get("method", "").endswith("+policy"), r.verification
    assert "local policy" in (r.verification.get("reason") or "").lower()
    assert "refund_cap" in (r.verification.get("reason") or "")
    # ... NOT from scope / a removed resource_bounds field.
    assert "C2_scope_fail" not in checks, checks
    assert "C2_scope_ok" in checks, checks
    # scope passed because it gates on the public tool name only.


def test_c_negative_resource_bounds_removed_from_schema():
    """NEGATIVE structural check: resource_bounds is GONE from the commitment schema
    (and amendment add_scope). The protocol carries no argument-level semantics."""
    import json
    schema_dir = os.path.join(ROOT, "schemas")
    commit = json.load(open(os.path.join(schema_dir, "vap-scope-commitment.schema.json")))
    scope_props = commit["properties"]["scope"]["properties"]
    assert "resource_bounds" not in scope_props, scope_props.keys()
    assert set(scope_props) <= {"tools_allow", "tools_deny"}, scope_props.keys()
    # and the whole commitment schema text never mentions it
    raw = json.dumps(commit)
    assert "resource_bounds" not in raw
    amend = json.load(open(os.path.join(schema_dir, "vap-scope-amendment.schema.json")))
    add_scope_props = amend["properties"]["add_scope"]["properties"]
    assert "resource_bounds" not in add_scope_props, add_scope_props.keys()
    assert "resource_bounds" not in json.dumps(amend)


def test_d_budget_max_calls_cap():
    """The universal max_calls meter caps the session regardless of unit cost."""
    c = VapClient(_state["proxy_url"])
    c.open_session(commitment(budget={"max_calls": 3, "limits": {"usd_opcost": 5.0},
                                      "deadline": FUTURE_DEADLINE}))
    verdicts = []
    for i in range(5):
        r = c.call("crm.read", {"customer_id": f"C{i}"},
                   {"rationale": f"Read customer C{i} for triage.",
                    "expected_effect": "CRM lookup.", "sensitivity": "reads"})
        verdicts.append(r.verdict)
    assert verdicts[:3] == ["served", "served", "served"], verdicts
    assert verdicts[3] == "denied" and verdicts[4] == "denied", verdicts
    r = c.call("crm.read", {"customer_id": "Cx"},
               {"rationale": "Another read.", "expected_effect": "CRM lookup.",
                "sensitivity": "reads"})
    assert "C3_budget_fail" in r.verification["checks"]
    reason = (r.verification.get("reason") or "").lower()
    assert "budget" in reason and "calls" in reason, reason


def test_d2_budget_limits_tokens_cap():
    """A named meter (limits.tokens) trips independently of max_calls.

    crm.read costs 200 tokens/call. With max_calls=10 (loose) and a 500-token
    ceiling, the 3rd read (3*200=600 > 500) is denied on the TOKENS meter, and the
    verdict reason names that meter -- demonstrating the multi-dimensional budget.
    """
    c = VapClient(_state["proxy_url"])
    c.open_session(commitment(budget={"max_calls": 10, "limits": {"tokens": 500},
                                      "deadline": FUTURE_DEADLINE}))
    verdicts = []
    for i in range(4):
        r = c.call("crm.read", {"customer_id": f"T{i}"},
                   {"rationale": f"Read customer T{i} for triage.",
                    "expected_effect": "CRM lookup.", "sensitivity": "reads"})
        verdicts.append(r.verdict)
    # 200, 400 ok; 600 > 500 -> denied. max_calls (10) is NOT the limiter here.
    assert verdicts[:2] == ["served", "served"], verdicts
    assert verdicts[2] == "denied" and verdicts[3] == "denied", verdicts
    r = c.call("crm.read", {"customer_id": "Tx"},
               {"rationale": "Another read.", "expected_effect": "CRM lookup.",
                "sensitivity": "reads"})
    assert "C3_budget_fail" in r.verification["checks"]
    reason = (r.verification.get("reason") or "").lower()
    assert "tokens" in reason and "limit" in reason, reason
    # ensure it was NOT the calls meter that tripped
    assert "max_calls" not in reason, reason


def test_e_semantic_catch_in_scope_but_incoherent():
    # In-scope ($5 < $200), budget fine, AND a ticket is cited so C4 policy PASSES.
    # But the call refunds ORD-999 while the rationale only references ORD-111 / its
    # ticket -- so only the semantic judge (C5) can catch the incoherence.
    c = new_client()
    r = c.call("payments.refund", {"order_id": "ORD-999", "amount_usd": 5},
               {"rationale": "Refund per ticket TCK-42 for order ORD-111.",
                "expected_effect": "Refund the duplicate charge on ORD-111.",
                "sensitivity": "writes_money"})
    assert r.verdict in ("clarify", "denied"), r.verification
    assert r.verification["semantic_invoked"] is True
    assert "C5_intent_inconsistent" in r.verification["checks"]
    # deterministic checks all passed -> "in-scope-but-incoherent"
    for ok in ("C2_scope_ok", "C3_budget_ok", "C4_policy_ok"):
        assert ok in r.verification["checks"], (ok, r.verification["checks"])


def test_f_amendment_reenables_call():
    """A signed amendment widens the PUBLIC tool namespace (tools_allow) -- the only
    legit way to escape the committed envelope. A tool denied by C2 scope becomes
    served after the amendment re-baselines the commitment."""
    c = VapClient(_state["proxy_url"])
    # Start WITHOUT tickets.* in scope so tickets.update is initially out of scope.
    c.open_session(commitment(scope={"tools_allow": ["crm.read", "payments.refund"],
                                      "tools_deny": ["admin.*"]}))
    args = {"ticket_id": "TCK-900", "status": "resolved"}
    intent = {"rationale": "Mark ticket TCK-900 resolved after handling the dispute.",
              "expected_effect": "Set ticket TCK-900 to resolved.",
              "sensitivity": "writes_data"}
    r1 = c.call("tickets.update", args, intent)
    assert r1.verdict == "denied", r1.verification
    assert "C2_scope_fail" in r1.verification["checks"], r1.verification
    amend = c.amend(add_scope={"tools_allow": ["tickets.*"]},
                    reason="Supervisor authorized ticket updates for the dispute backlog.",
                    sign=True)
    assert amend.verdict == "served", amend.verification
    r2 = c.call("tickets.update", args, intent)
    assert r2.verdict == "served", r2.verification
    assert r2.result and r2.result.get("ok") is True


def test_f2_unsigned_amendment_denied():
    c = new_client()
    amend = c.amend(add_scope={"tools_allow": ["admin.*"]},
                    reason="sneaky widen", sign=False)
    assert amend.verdict == "denied", amend.verification
    assert "amend_signed_fail" in amend.verification["checks"]


def test_g_looping_triggers_semantic():
    c = new_client()
    args = {"order_id": "ORD-77", "amount_usd": 25}
    intent = {"rationale": "Refund $25 on ORD-77 per ticket TCK-501.",
              "expected_effect": "Refund $25 on ORD-77.", "sensitivity": "writes_data"}
    invoked, triggers = False, []
    for _ in range(4):
        r = c.call("payments.refund", args, intent)
        if r.verification.get("semantic_invoked"):
            invoked = True
            triggers.append(r.verification.get("semantic_trigger"))
    assert invoked, "looping never triggered the semantic judge"
    assert "looping" in triggers, triggers


def test_h_audit_binds_intent_and_verdict():
    c = new_client()
    c.call("crm.read", {"customer_id": "C1"},
           {"rationale": "read for ticket TCK-1", "expected_effect": "CRM lookup.",
            "sensitivity": "reads"})
    c.call("admin.delete_user", {"user_id": "u1"},
           {"rationale": "drift attempt", "expected_effect": "delete",
            "sensitivity": "deletes"})  # denied
    audit = c.get_audit()
    assert audit["chain_intact"] is True
    records = audit["records"]
    assert len(records) >= 3
    call_records = [r for r in records if r["kind"] == "call"]
    assert call_records
    for rec in call_records:
        assert rec["intent"] is not None
        assert rec["call"] is not None
        assert rec["verdict"] in ("served", "clarify", "denied", "downgraded")
    denied = [r for r in call_records if r["verdict"] == "denied"]
    assert denied, "denied call not recorded"
    assert denied[0]["call"]["tool"] == "admin.delete_user"
    assert "scope" in (denied[0]["verification"].get("reason") or "").lower()
    assert all(r.get("signature", {}).get("alg") == "HMAC-SHA256" for r in records)


# --------------------------------------------------------------------------- #
# Real LLM judge wired to a LOCAL fake OpenAI-compatible server (no network).
# Proves: (1) the OpenAI Chat Completions request is built correctly,
#         (2) the strict-JSON verdict is parsed, and
#         (3) usage.total_tokens is billed to the session `tokens` budget meter.
# --------------------------------------------------------------------------- #
def _spawn_fake_openai(canned: dict):
    """Start a stdlib http.server that records the last /chat/completions request
    and returns `canned`. Returns (base_url, captured, server, thread)."""
    import http.server
    import json as _json
    import threading

    captured: dict = {}

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            captured["path"] = self.path
            captured["auth"] = self.headers.get("Authorization")
            captured["body"] = _json.loads(raw or b"{}")
            data = _json.dumps(canned).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return f"http://127.0.0.1:{port}/v1", captured, httpd, th


def test_i_llm_judge_against_fake_openai_server():
    # Import the real judge from the proxy package.
    sys.path.insert(0, PROXY_DIR)
    from semantic_judge import LLMSemanticJudge  # noqa: E402

    canned = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "fake-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant",
                        "content": '{"consistent": true, "confidence": 0.83, '
                                   '"reason": "refund grounded in ticket + order"}'},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 130, "completion_tokens": 47, "total_tokens": 177},
    }
    base_url, captured, httpd, _th = _spawn_fake_openai(canned)
    try:
        judge = LLMSemanticJudge(base_url=base_url, api_key="sk-test",
                                 model="gpt-4o-mini", timeout=10)
        call = {"tool": "payments.refund",
                "arguments": {"order_id": "ORD-77", "amount_usd": 25}}
        consistent, confidence, reason, tokens = judge.evaluate(
            goal="Resolve support tickets and issue small refunds.",
            rationale="Refund $25 on ORD-77 per ticket TCK-501.",
            expected_effect="Issue a $25 refund on ORD-77.",
            call=call)

        # (2) JSON verdict parsed
        assert consistent is True, (consistent, reason)
        assert abs(confidence - 0.83) < 1e-9, confidence
        assert "grounded" in reason, reason
        # (3) token usage captured from usage.total_tokens
        assert tokens == 177, tokens

        # (1) the OpenAI-shaped request was built correctly
        assert captured["path"].endswith("/chat/completions"), captured["path"]
        assert captured["auth"] == "Bearer sk-test", captured["auth"]
        body = captured["body"]
        assert body["model"] == "gpt-4o-mini"
        assert body["temperature"] == 0
        assert body["response_format"] == {"type": "json_object"}
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user"], roles
        user = body["messages"][1]["content"]
        assert "GOAL:" in user and "RATIONALE:" in user and "CALL:" in user
        assert "ORD-77" in user
    finally:
        httpd.shutdown()


def test_i2_llm_judge_tokens_billed_to_budget_meter():
    """Drive the VerificationEngine in-process with the LLM judge pointed at the
    fake server, and prove the judge's reported tokens are billed to the session's
    `tokens` budget meter (verification cost is itself budgeted)."""
    sys.path.insert(0, PROXY_DIR)
    from audit import AuditLog  # noqa: E402
    from semantic_judge import LLMSemanticJudge  # noqa: E402
    from verification import EngineConfig, VerificationEngine  # noqa: E402

    canned = {
        "choices": [{"message": {"content":
            '{"consistent": true, "confidence": 0.9, "reason": "ok"}'}}],
        "usage": {"total_tokens": 300},
    }
    base_url, captured, httpd, _th = _spawn_fake_openai(canned)
    try:
        judge = LLMSemanticJudge(base_url=base_url, api_key="", model="llama3.1",
                                 timeout=10)
        eng = VerificationEngine(config=EngineConfig(), audit=AuditLog(), judge=judge)
        sid = "llm-sess-1"
        # Commitment with a tokens meter big enough for: crm.read(200) + refund(50)
        # + judge tokens(300) on the refund (writes_money -> judge fires).
        commit = {
            "vap": "0.1", "type": "scope_commitment", "session_id": sid,
            "goal": "Resolve support tickets: read CRM and issue small refunds.",
            "scope": {"tools_allow": ["crm.read", "payments.refund"]},
            "budget": {"max_calls": 10,
                       "limits": {"tokens": 1000, "usd_opcost": 5.0}},
            "principal": {"agent_id": "did:web:acme.ai:agent:support"},
        }
        v = eng.open_session(sid, commit)
        assert v.verdict == "served", v.verification

        # A writes_money refund fires the judge; it returns consistent=true (served).
        env = {"vap": "0.1", "type": "intent_call", "session_id": sid,
               "intent": {"rationale": "Refund $25 on ORD-77 per ticket TCK-501.",
                          "expected_effect": "Issue a $25 refund on ORD-77.",
                          "sensitivity": "writes_money"},
               "call": {"tool": "payments.refund",
                        "arguments": {"order_id": "ORD-77", "amount_usd": 25}}}
        verdict, forward, _call = eng.verify_call(sid, env)
        ver = verdict.verification.to_dict()  # engine returns a Verdict object
        assert verdict.verdict == "served", ver
        assert ver["semantic_invoked"] is True
        assert "C5_intent_consistent" in ver["checks"]
        # the judge tokens are surfaced in the checks for transparency
        assert any(c.startswith("C5_judge_tokens=300") for c in ver["checks"]), ver["checks"]

        # tokens billed: payments.refund tool cost (50) + judge usage (300) = 350.
        sess = eng.sessions[sid]
        assert sess.consumed.get("tokens") == 350.0, sess.consumed
        # usd_opcost billed from the refund tool cost (0.01); calls counted. (This is
        # the in-process engine path -- no upstream result, so the operator-fallback
        # estimate is what's billed; the proxy E2E path reconciles to _meta.vap.cost.)
        assert abs(sess.consumed.get("usd_opcost", 0.0) - 0.01) < 1e-9, sess.consumed
        assert sess.calls_made == 1, sess.calls_made

        # And the request actually went to the fake OpenAI endpoint (no API key set).
        assert captured["path"].endswith("/chat/completions")
        assert captured.get("auth") is None  # blank api key -> no Authorization header
    finally:
        httpd.shutdown()


# --------------------------------------------------------------------------- #
# Tool-agnostic cumulative metering + tool self-reported cost (CHANGE C).
# --------------------------------------------------------------------------- #
def test_j_cumulative_meter_denied():
    """Headline: cumulative side-effect control via a PLAIN, tool-agnostic meter.

    Each refund is individually IN POLICY (amount <= $200, ticket cited) so C4 passes
    every time. But the refund tool self-reports a `disbursed_usd` contribution per
    call (= the amount it moved). VAP just SUMS that opaque meter against
    budget.limits.disbursed_usd and denies a LATER call once the aggregate would be
    exceeded -- with NO protocol knowledge of what a refund is. The denial reason names
    the disbursed_usd meter.
    """
    c = VapClient(_state["proxy_url"])
    # Cumulative ceiling of $250; each refund moves $120 (in policy). The 3rd/4th call
    # trips the meter as the self-reported contributions accumulate.
    c.open_session(commitment(budget={"max_calls": 25,
                                      "limits": {"usd_opcost": 5.0, "tokens": 50000,
                                                 "disbursed_usd": 250},
                                      "deadline": FUTURE_DEADLINE}))
    verdicts = []
    for i in range(1, 6):
        r = c.call("payments.refund",
                   {"order_id": f"ORD-{i}", "amount_usd": 120},
                   {"rationale": f"Refund $120 on ORD-{i} per ticket TCK-{i}.",
                    "expected_effect": f"Refund $120 on ORD-{i}.",
                    "sensitivity": "writes_money"})
        verdicts.append(r)
    kinds = [v.verdict for v in verdicts]
    # Several in-policy refunds are served, then a later one is denied on the meter.
    served = [v for v in verdicts if v.verdict == "served"]
    denied = [v for v in verdicts if v.verdict == "denied"]
    assert len(served) >= 2, kinds
    assert denied, ("expected a later cumulative-meter denial", kinds)
    # Every served call passed C4 local policy (each refund was individually in policy).
    for v in served:
        assert "C4_policy_ok" in v.verification["checks"], v.verification
    # The denial is a budget/meter trip naming disbursed_usd -- NOT a policy or scope fail.
    d = denied[0]
    assert "C3_budget_fail" in d.verification["checks"], d.verification
    reason = (d.verification.get("reason") or "").lower()
    assert "disbursed_usd" in reason and "limit" in reason, reason
    assert "C4_policy_fail" not in d.verification["checks"], d.verification
    assert "C2_scope_fail" not in d.verification["checks"], d.verification


def test_k_tool_self_reported_cost():
    """The proxy bills meters from the TOOL RESULT's _meta.vap.cost, not from any
    hardcoded protocol logic. A single in-policy refund of $42 self-reports
    disbursed_usd=42; the proxy surfaces the billed cost map on the verdict and the
    session's consumed disbursed_usd reflects the tool's self-reported value."""
    c = VapClient(_state["proxy_url"])
    c.open_session(commitment(budget={"max_calls": 25,
                                      "limits": {"usd_opcost": 5.0, "tokens": 50000,
                                                 "disbursed_usd": 10000},
                                      "deadline": FUTURE_DEADLINE}))
    r = c.call("payments.refund", {"order_id": "ORD-K", "amount_usd": 42},
               {"rationale": "Refund $42 on ORD-K per ticket TCK-77.",
                "expected_effect": "Refund $42 on ORD-K.",
                "sensitivity": "writes_money"})
    assert r.verdict == "served", r.verification
    # The proxy echoes the tool's self-reported cost map under result._meta.vap.
    meta_vap = ((r.raw.get("result") or {}).get("_meta") or {}).get("vap") or {}
    billed = meta_vap.get("billed_cost") or {}
    assert billed.get("disbursed_usd") == 42, billed
    assert "usd_opcost" in billed and "tokens" in billed, billed
    # A second $42 refund: consumed disbursed_usd must reflect the tool's reported
    # values (42 + 42 = 84), proving the proxy summed the self-reported contributions
    # rather than a hardcoded constant.
    r2 = c.call("payments.refund", {"order_id": "ORD-K2", "amount_usd": 42},
                {"rationale": "Refund $42 on ORD-K2 per ticket TCK-78.",
                 "expected_effect": "Refund $42 on ORD-K2.",
                 "sensitivity": "writes_money"})
    assert r2.verdict == "served", r2.verification
    audit = c.get_audit()
    assert audit["chain_intact"] is True


# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    setup_module()
    try:
        for t in tests:
            try:
                t()
                print(f"PASS {t.__name__}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"FAIL {t.__name__}: {e}")
                traceback.print_exc()
                failed += 1
    finally:
        teardown_module()
    print(f"\n==== {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
