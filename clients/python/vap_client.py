"""Thin VAP client (Python, stdlib only).

Builds the _meta.vap payloads, speaks MCP JSON-RPC 2.0 over HTTP to the VAP proxy,
and parses verdicts. Contains NO verification logic -- decisions are server-side.
The surface mirrors the TypeScript binding.

    c = VapClient("http://localhost:9000")
    c.open_session(commitment)                # initialize + scope_commitment
    res = c.call("payments.refund", {...}, intent={...})
    res.verdict                               # served|clarify|downgraded|denied
    c.amend(add_scope={...}, reason="...", sign=True)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

VAP_VERSION = "0.1"
SESSION_HEADER = "mcp-session-id"


@dataclass
class VapResult:
    verdict: str
    verification: Dict[str, Any]
    clarification: Optional[Dict[str, Any]]
    audit_ref: Optional[str]
    accepted_commitment_digest: Optional[str]
    result: Optional[Dict[str, Any]]
    raw: Dict[str, Any]

    @property
    def served(self) -> bool:
        return self.verdict == "served"


class VapClient:
    def __init__(self, base_url: str, hmac_secret: str = "vap-dev-secret"):
        self.base_url = base_url.rstrip("/")
        self.session_id: Optional[str] = None
        self.commitment_digest: Optional[str] = None
        self._hmac_secret = hmac_secret.encode()
        self._rpc_id = 0

    # -- HMAC helper ---------------------------------------------------- #
    def sign(self, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        mac = hmac.new(self._hmac_secret, canonical.encode(), hashlib.sha256).hexdigest()
        return f"hmac:{mac}"

    # -- transport ------------------------------------------------------ #
    def _post(self, rpc: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        data = json.dumps(rpc).encode()
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        last_err = None
        for _attempt in range(3):  # tolerate transient connection resets at startup
            try:
                req = urllib.request.Request(self.base_url + "/mcp", data=data,
                                             headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    new_sid = resp.headers.get(SESSION_HEADER)
                    body = json.loads(resp.read().decode())
                return body, new_sid
            except (ConnectionError, OSError) as e:  # noqa: PERF203
                last_err = e
                import time as _t
                _t.sleep(0.1)
        raise last_err  # type: ignore[misc]

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    # -- API ------------------------------------------------------------ #
    def open_session(self, commitment: Dict[str, Any], sign: bool = False) -> VapResult:
        c = dict(commitment)
        c.setdefault("vap", VAP_VERSION)
        c.setdefault("type", "scope_commitment")
        # session_id is filled by the proxy on initialize; placeholder accepted
        c.setdefault("session_id", "pending")
        if sign and "signature" not in c:
            c["signature"] = self.sign({k: v for k, v in c.items() if k != "signature"})
        rpc = {"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
               "params": {"protocolVersion": "2024-11-05",
                          "clientInfo": {"name": "vap-python-client", "version": "0.1.0"},
                          "_meta": {"vap": {"scope_commitment": c}}}}
        body, new_sid = self._post(rpc)
        if new_sid:
            self.session_id = new_sid
        res = self._parse(body)
        if res.accepted_commitment_digest:
            self.commitment_digest = res.accepted_commitment_digest
        return res

    def call(self, tool: str, arguments: Dict[str, Any], intent: Dict[str, Any]) -> VapResult:
        envelope = {"vap": VAP_VERSION, "type": "intent_call",
                    "session_id": self.session_id, "intent": dict(intent),
                    "call": {"tool": tool, "arguments": arguments}}
        rpc = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
               "params": {"name": tool, "arguments": arguments,
                          "_meta": {"vap": {"intent": envelope}}}}
        body, _ = self._post(rpc)
        return self._parse(body)

    def amend(self, *, add_scope: Optional[Dict[str, Any]] = None,
              increase_budget: Optional[Dict[str, Any]] = None,
              reason: str = "re-planning", new_plan_digest: Optional[str] = None,
              sign: bool = True) -> VapResult:
        payload: Dict[str, Any] = {
            "vap": VAP_VERSION, "type": "scope_amendment",
            "session_id": self.session_id,
            "prev_commitment_digest": self.commitment_digest or "sha256:0",
            "reason": reason,
        }
        if add_scope is not None:
            payload["add_scope"] = add_scope
        if increase_budget is not None:
            payload["increase_budget"] = increase_budget
        if new_plan_digest is not None:
            payload["new_plan_digest"] = new_plan_digest
        if sign:
            payload["signature"] = self.sign(
                {k: v for k, v in payload.items() if k != "signature"})
        rpc = {"jsonrpc": "2.0", "id": self._next_id(), "method": "vap/amend",
               "params": {"_meta": {"vap": {"amendment": payload}}, **payload}}
        body, _ = self._post(rpc)
        res = self._parse(body)
        if res.accepted_commitment_digest:
            self.commitment_digest = res.accepted_commitment_digest
        return res

    def get_audit(self) -> Dict[str, Any]:
        url = self.base_url + "/audit"
        if self.session_id:
            url += "?session_id=" + self.session_id
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode())

    # -- parsing -------------------------------------------------------- #
    @staticmethod
    def _parse(body: Dict[str, Any]) -> VapResult:
        result = body.get("result", {}) or {}
        meta = (result.get("_meta") or {}).get("vap") or {}
        v = meta.get("verdict") or {}
        return VapResult(
            verdict=v.get("verdict", "denied" if "error" in body else "unknown"),
            verification=v.get("verification", {}),
            clarification=v.get("clarification"),
            audit_ref=v.get("audit_ref"),
            accepted_commitment_digest=v.get("accepted_commitment_digest"),
            result=result.get("structuredContent"),
            raw=body,
        )
