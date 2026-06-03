"""Declarative multi-step workflows driven against the VAP proxy.

Each workflow is an ordered list of steps: an ``open`` (scope commitment) implicitly
first, then ``call`` (intent + tool) and ``amend`` steps. The runner prints the
verdict per step so the demo shows VAP gating live.
"""

from __future__ import annotations

FUTURE_DEADLINE = "2099-01-01T00:00:00Z"

SUPPORT_COMMITMENT = {
    "goal": "Resolve customer support tickets: read CRM, refund small amounts, and "
            "update/escalate tickets.",
    "scope": {
        # Public tool namespace only -- no argument semantics (those are C4 local policy).
        "tools_allow": ["crm.read", "payments.refund", "tickets.*"],
        "tools_deny": ["admin.*"],
    },
    "budget": {"max_calls": 25,
               # usd_opcost = operational cost; disbursed_usd = a cumulative business
               # side-effect meter the refund tool self-reports (tool-agnostic).
               "limits": {"usd_opcost": 1.0, "tokens": 20000, "disbursed_usd": 400},
               "deadline": FUTURE_DEADLINE},
    "plan_digest": "sha256:plansupportv1",
    "principal": {"agent_id": "did:web:acme.ai:agent:support",
                  "delegated_by": "user-42"},
}

WORKFLOWS = {
    "happy_support": {
        "commitment": SUPPORT_COMMITMENT,
        "steps": [
            {"type": "call", "tool": "crm.read", "arguments": {"customer_id": "C100"},
             "intent": {"rationale": "Look up customer C100 to verify the order on "
                                     "ticket TCK-501.",
                        "expected_effect": "Read-only CRM lookup.", "step": 1,
                        "sensitivity": "reads"}},
            {"type": "call", "tool": "payments.refund",
             "arguments": {"order_id": "ORD-77", "amount_usd": 25},
             "intent": {"rationale": "Refund $25 for order ORD-77 per ticket TCK-501 "
                                     "(item arrived damaged).",
                        "expected_effect": "Issue a $25 refund on order ORD-77.",
                        "step": 2, "sensitivity": "writes_money"}},
            {"type": "call", "tool": "tickets.update",
             "arguments": {"ticket_id": "TCK-501", "status": "resolved"},
             "intent": {"rationale": "Mark ticket TCK-501 resolved after refunding "
                                     "ORD-77.",
                        "expected_effect": "Set ticket status to resolved.", "step": 3,
                        "sensitivity": "writes_data"}},
        ],
    },
    "drift_delete": {
        "commitment": SUPPORT_COMMITMENT,
        "steps": [
            {"type": "call", "tool": "admin.delete_user",
             "arguments": {"user_id": "user-42"},
             "intent": {"rationale": "Clean up the account while I'm here.",
                        "expected_effect": "Delete user-42.", "step": 1,
                        "sensitivity": "deletes"}},
        ],
    },
    # Demonstrates (1) a call denied by a LOCAL POLICY rule (refund_cap: amount > $200),
    # NOT by any protocol field, and (2) a signed amendment that raises a universal
    # budget meter so legit re-planning can proceed.
    "policy_deny_then_amend": {
        "commitment": SUPPORT_COMMITMENT,
        "steps": [
            {"type": "call", "tool": "payments.refund",
             "arguments": {"order_id": "ORD-88", "amount_usd": 300},
             "intent": {"rationale": "Refund $300 for order ORD-88 per ticket TCK-900.",
                        "expected_effect": "Refund $300 on ORD-88.", "step": 1,
                        "sensitivity": "writes_money"}},  # denied by C4 local policy
            {"type": "amend",
             "increase_budget": {"add_limits": {"disbursed_usd": 200}},
             "reason": "Supervisor approved a higher cumulative disbursement ceiling "
                       "for the dispute backlog (TCK-900)."},
            {"type": "call", "tool": "payments.refund",
             "arguments": {"order_id": "ORD-12", "amount_usd": 150},
             "intent": {"rationale": "Refund $150 for ORD-12 per ticket TCK-901 "
                                     "(in policy: <= $200, ticket cited).",
                        "expected_effect": "Refund $150 on ORD-12.", "step": 2,
                        "sensitivity": "writes_money"}},
        ],
    },
    # Demonstrates the CUMULATIVE side-effect meter: each refund is individually in
    # policy (<= $200, ticket cited) but their self-reported disbursed_usd contributions
    # sum past budget.limits.disbursed_usd, tripping the meter -- with NO protocol
    # knowledge of what a refund is.
    "cumulative_disbursement": {
        "commitment": {**SUPPORT_COMMITMENT,
                       "budget": {"max_calls": 25,
                                  "limits": {"usd_opcost": 1.0, "tokens": 20000,
                                             "disbursed_usd": 250},
                                  "deadline": FUTURE_DEADLINE}},
        "steps": [
            {"type": "call", "tool": "payments.refund",
             "arguments": {"order_id": f"ORD-{i}", "amount_usd": 120},
             "intent": {"rationale": f"Refund $120 for ORD-{i} per ticket TCK-{i} "
                                     "(in policy).",
                        "expected_effect": f"Refund $120 on ORD-{i}.", "step": i,
                        "sensitivity": "writes_money"}}
            for i in range(1, 5)
        ],
    },
}
