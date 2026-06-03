"""Scripted VAP agent: runs the declarative workflows against the proxy.

Used as the ``agent`` service in docker-compose (depends_on vap-proxy). Drives the
proxy with the Python reference client and prints each verdict so you can watch VAP
serve / deny / clarify / re-baseline in real time.
"""

from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "clients", "python"))
sys.path.insert(0, "/app/clients/python")
sys.path.insert(0, HERE)

from vap_client import VapClient   # noqa: E402
from workflows import WORKFLOWS     # noqa: E402


def run_workflow(base_url: str, name: str) -> None:
    wf = WORKFLOWS[name]
    print(f"\n=== workflow: {name} ===", flush=True)
    client = VapClient(base_url)
    init = client.open_session(wf["commitment"])
    print(f"  [init]   verdict={init.verdict} session={client.session_id} "
          f"reason={init.verification.get('reason')}", flush=True)
    for step in wf["steps"]:
        if step["type"] == "call":
            r = client.call(step["tool"], step["arguments"], step["intent"])
            v = r.verification
            print(f"  [call]   {step['tool']:<20} -> {r.verdict:<9} "
                  f"risk={v.get('risk_score')} semantic={v.get('semantic_invoked')} "
                  f"trigger={v.get('semantic_trigger')} reason={v.get('reason')}",
                  flush=True)
        elif step["type"] == "amend":
            r = client.amend(add_scope=step.get("add_scope"),
                             increase_budget=step.get("increase_budget"),
                             reason=step.get("reason", "re-planning"), sign=True)
            print(f"  [amend]  -> {r.verdict:<9} reason={r.verification.get('reason')}",
                  flush=True)
    audit = client.get_audit()
    print(f"  [audit]  {len(audit.get('records', []))} records, "
          f"chain_intact={audit.get('chain_intact')}", flush=True)


def main() -> None:
    base_url = os.getenv("PROXY_URL", "http://localhost:9000")
    import urllib.request
    for _ in range(30):
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(1)
    for name in ("happy_support", "drift_delete", "policy_deny_then_amend",
                 "cumulative_disbursement"):
        run_workflow(base_url, name)
    print("\n[agent] workflows complete.", flush=True)


if __name__ == "__main__":
    main()
