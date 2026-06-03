"""VAP proxy HTTP server.

Speaks MCP JSON-RPC 2.0 on both sides. VAP rides inside MCP's params._meta.vap --
no non-MCP transport is invented. The proxy:

* intercepts initialize  -> S1+S2 on _meta.vap.scope_commitment
* intercepts tools/call  -> C1..C5 on _meta.vap.intent; on `served` forwards a
                            CLEAN MCP call upstream (UPSTREAM_URL) and wraps the
                            upstream result with the VAP verdict in
                            result._meta.vap.verdict
* intercepts vap/amend   -> re-baselines the commitment
* exposes GET /audit     -> append-only audit records (for tests)

FastAPI+uvicorn when installed; else stdlib http.server. Upstream calls use stdlib
urllib so the test path needs zero PyPI packages.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audit import AuditLog
from config import load_config
from models import DENIED
from semantic_judge import get_judge
from verification import EngineConfig, VerificationEngine

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://localhost:8000/mcp")
SESSION_HEADER = "mcp-session-id"


def _meta_vap(params: Dict[str, Any]) -> Dict[str, Any]:
    return ((params or {}).get("_meta") or {}).get("vap") or {}


class Proxy:
    def __init__(self, engine=None, upstream_url=None):
        self.engine = engine or self._default_engine()
        self.upstream_url = upstream_url or UPSTREAM_URL
        self._session_counter = 0

    @staticmethod
    def _default_engine() -> VerificationEngine:
        cfg = load_config(os.getenv("VAP_CONFIG", "vap-gateway.yaml"))
        return VerificationEngine(config=EngineConfig.from_dict(cfg),
                                  audit=AuditLog(), judge=get_judge())

    def _new_session_id(self) -> str:
        self._session_counter += 1
        return f"vap-sess-{self._session_counter:04d}"

    def _forward(self, rpc: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(self.upstream_url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def _strip(rpc: Dict[str, Any]) -> Dict[str, Any]:
        clean = json.loads(json.dumps(rpc))
        params = clean.get("params") or {}
        meta = params.get("_meta")
        if meta and "vap" in meta:
            del meta["vap"]
            if not meta:
                params.pop("_meta", None)
        return clean

    # ------------------------------------------------------------------ #
    def dispatch(self, rpc: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
        rid = rpc.get("id")
        method = rpc.get("method")
        params = rpc.get("params", {}) or {}
        vap = _meta_vap(params)

        def jr(result):
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        if method == "initialize":
            commitment = vap.get("scope_commitment")
            sid = session_id or self._new_session_id()
            upstream = self._forward(self._strip(rpc))
            res = upstream.get("result", {"protocolVersion": "2024-11-05"})
            if commitment is None:
                res.setdefault("_meta", {})["vap"] = {
                    "session_id": sid,
                    "verdict": {"vap": "0.1", "type": "verdict", "session_id": sid,
                                "verdict": "served",
                                "verification": {"checks": ["no_commitment"],
                                                 "method": "static",
                                                 "semantic_invoked": False,
                                                 "reason": "no scope_commitment supplied"}}}
            else:
                verdict = self.engine.open_session(sid, commitment)
                res.setdefault("_meta", {})["vap"] = {"session_id": sid,
                                                      "verdict": verdict.to_dict()}
            return {"jsonrpc": "2.0", "id": rid, "result": res, "_session_id": sid}

        if method == "vap/amend":
            payload = vap.get("amendment") or params.get("amendment") or params
            if "session_id" not in payload and session_id:
                payload = {**payload, "session_id": session_id}
            verdict = self.engine.amend(payload)
            return jr({"_meta": {"vap": {"verdict": verdict.to_dict()}}})

        if method == "tools/list":
            return self._forward(self._strip(rpc))

        if method == "tools/call":
            sid = session_id or ""
            intent_env = vap.get("intent") or {}
            if "session_id" not in intent_env and sid:
                intent_env = {**intent_env, "session_id": sid}
            verdict, forward, _call = self.engine.verify_call(sid, intent_env)
            if not forward:
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"isError": verdict.verdict == DENIED,
                                   "content": [{"type": "text",
                                                "text": f"VAP {verdict.verdict}: "
                                                        f"{verdict.verification.reason}"}],
                                   "_meta": {"vap": {"verdict": verdict.to_dict()}}}}
            upstream = self._forward(self._strip(rpc))
            if "error" in upstream:
                return {"jsonrpc": "2.0", "id": rid, "error": upstream["error"]}
            res = upstream.get("result", {})
            # CHANGE C: bill the TOOL's self-reported per-call meter contributions.
            # The upstream MCP result MAY carry result._meta.vap.cost (an opaque map of
            # named meters -> contribution). The proxy reconciles it against the
            # operator-fallback estimate already billed -- it never hardcodes any
            # tool->meter knowledge; meter semantics live in the tool/operator.
            reported_cost = (((res.get("_meta") or {}).get("vap") or {}).get("cost"))
            if isinstance(reported_cost, dict):
                self.engine.reconcile_cost(sid, verdict.audit_ref or "", reported_cost)
            # Replace any upstream vap meta with our verdict (do not leak the cost map
            # back unchanged; surface it under the verdict's view instead).
            res.setdefault("_meta", {})["vap"] = {"verdict": verdict.to_dict()}
            if isinstance(reported_cost, dict):
                res["_meta"]["vap"]["billed_cost"] = reported_cost
            self.engine.record_result(sid, verdict.audit_ref or "",
                                      res.get("structuredContent"))
            return {"jsonrpc": "2.0", "id": rid, "result": res}

        if method in ("notifications/initialized", "ping"):
            return jr({})
        return self._forward(self._strip(rpc))

    def audit_records(self, session_id=None):
        return self.engine.audit.all(session_id)


def build_fastapi_app(proxy=None):  # pragma: no cover - Docker only
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    proxy = proxy or Proxy()
    app = FastAPI(title="vap-proxy")

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "vap-proxy"}

    @app.get("/audit")
    async def audit(session_id: Optional[str] = None):
        return {"records": proxy.audit_records(session_id),
                "chain_intact": proxy.engine.audit.verify_chain()}

    @app.post("/mcp")
    async def mcp(request: Request):
        body = await request.json()
        sid = request.headers.get(SESSION_HEADER)
        out = proxy.dispatch(body, sid)
        new_sid = out.pop("_session_id", None)
        headers = {SESSION_HEADER: new_sid} if new_sid else {}
        return JSONResponse(out, headers=headers)

    return app


def run_stdlib(proxy, host="0.0.0.0", port=9000):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, extra_headers=None):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self._send(200, {"ok": True, "service": "vap-proxy"})
            if parsed.path == "/audit":
                q = parse_qs(parsed.query)
                sid = q.get("session_id", [None])[0]
                return self._send(200, {"records": proxy.audit_records(sid),
                                        "chain_intact": proxy.engine.audit.verify_chain()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/mcp":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            sid = self.headers.get(SESSION_HEADER)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
            out = proxy.dispatch(body, sid)
            new_sid = out.pop("_session_id", None)
            self._send(200, out, {SESSION_HEADER: new_sid} if new_sid else None)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "9000"))
    proxy = Proxy()
    try:
        import uvicorn  # type: ignore
        uvicorn.run(build_fastapi_app(proxy), host=host, port=port, log_level="warning")
    except ImportError:
        httpd = run_stdlib(proxy, host, port)
        print(f"[vap-proxy] stdlib http.server on {host}:{port} -> upstream "
              f"{proxy.upstream_url}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
