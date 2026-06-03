"""Minimal MCP server (JSON-RPC 2.0 over HTTP, single POST /mcp endpoint).

Implements initialize, tools/list, tools/call. Tools return simple JSON. This is
the upstream the VAP proxy forwards permitted calls to. It knows nothing about VAP;
it ignores params._meta (MCP passthrough).

Runs on FastAPI+uvicorn when installed (Docker image); otherwise stdlib http.server
so it runs in a bare sandbox with zero PyPI deps.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

SERVER_INFO = {"name": "vap-demo-mcp-server", "version": "0.1.0"}

TOOLS = [
    {"name": "crm.read", "description": "Read a customer record from the CRM (read-only).",
     "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}},
                     "required": ["customer_id"]}, "_effect": "read"},
    {"name": "payments.refund", "description": "Issue a refund against an order (moves money).",
     "inputSchema": {"type": "object",
                     "properties": {"order_id": {"type": "string"},
                                    "amount_usd": {"type": "number"}},
                     "required": ["order_id", "amount_usd"]}, "_effect": "writes_money"},
    {"name": "tickets.update", "description": "Update a support ticket (writes data).",
     "inputSchema": {"type": "object",
                     "properties": {"ticket_id": {"type": "string"},
                                    "status": {"type": "string"}},
                     "required": ["ticket_id"]}, "_effect": "writes_data"},
    {"name": "tickets.escalate", "description": "Escalate a support ticket to tier-2 (writes data).",
     "inputSchema": {"type": "object",
                     "properties": {"ticket_id": {"type": "string"},
                                    "reason": {"type": "string"}},
                     "required": ["ticket_id"]}, "_effect": "writes_data"},
    {"name": "admin.delete_user", "description": "Permanently delete a user account (destructive).",
     "inputSchema": {"type": "object", "properties": {"user_id": {"type": "string"}},
                     "required": ["user_id"]}, "_effect": "deletes"},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _self_reported_cost(name: str, args: Dict[str, Any], result: Dict[str, Any]
                        ) -> Dict[str, float]:
    """A TOOL self-declares its opaque per-call contribution to named budget meters.

    VAP treats these names as opaque -- it only sums and enforces ceilings. The refund
    tool reports the business `disbursed_usd` it actually moved (from its own result),
    plus operational `usd_opcost` and `tokens`. This is how cumulative side-effects are
    bounded WITHOUT the protocol knowing what any meter (or tool) means.
    """
    if name == "payments.refund":
        amt = result.get("amount_usd")
        cost: Dict[str, float] = {"usd_opcost": 0.01, "tokens": 50}
        if isinstance(amt, (int, float)):
            cost["disbursed_usd"] = float(amt)
        return cost
    if name == "crm.read":
        return {"tokens": 200}
    if name in ("tickets.update", "tickets.escalate"):
        return {"tokens": 120}
    if name == "admin.delete_user":
        return {"usd_opcost": 0.0, "tokens": 80}
    return {"tokens": 100}


def _execute(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "crm.read":
        cid = args.get("customer_id", "unknown")
        return {"customer_id": cid, "name": f"Customer {cid}", "tier": "gold",
                "open_orders": [f"ORD-{cid}-1"]}
    if name == "payments.refund":
        return {"refund_id": f"RF-{args.get('order_id', 'X')}",
                "order_id": args.get("order_id"), "amount_usd": args.get("amount_usd"),
                "status": "refunded"}
    if name == "tickets.update":
        return {"ticket_id": args.get("ticket_id"),
                "status": args.get("status", "updated"), "ok": True}
    if name == "tickets.escalate":
        return {"ticket_id": args.get("ticket_id"), "escalated_to": "tier-2", "ok": True}
    if name == "admin.delete_user":
        return {"user_id": args.get("user_id"), "deleted": True}
    raise KeyError(name)


def handle_rpc(req: Dict[str, Any]) -> Dict[str, Any]:
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params", {}) or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05", "serverInfo": SERVER_INFO,
                   "capabilities": {"tools": {}}})
    if method == "tools/list":
        public = [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]
        return ok({"tools": public})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name not in _BY_NAME:
            return err(-32602, f"unknown tool: {name}")
        try:
            result = _execute(name, args)
        except Exception as exc:  # pragma: no cover
            return err(-32000, f"tool error: {exc}")
        # The tool self-declares its opaque per-call meter contribution under
        # result._meta.vap.cost. The VAP proxy reads & bills this (it never hardcodes
        # tool->meter knowledge); a plain MCP client simply ignores _meta.
        cost = _self_reported_cost(name, args, result)
        return ok({"content": [{"type": "text", "text": json.dumps(result)}],
                   "structuredContent": result, "isError": False,
                   "_meta": {"vap": {"cost": cost}}})
    if method in ("notifications/initialized", "ping"):
        return ok({})
    return err(-32601, f"method not found: {method}")


def build_fastapi_app():  # pragma: no cover - Docker only
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    app = FastAPI(title="vap-demo-mcp-server")

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "mcp-server"}

    @app.post("/mcp")
    async def mcp(request: Request):
        body = await request.json()
        if isinstance(body, list):
            return JSONResponse([handle_rpc(r) for r in body])
        return JSONResponse(handle_rpc(body))

    return app


def run_stdlib(host="0.0.0.0", port=8000):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "service": "mcp-server"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/mcp":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
            if isinstance(body, list):
                return self._send(200, [handle_rpc(r) for r in body])
            self._send(200, handle_rpc(body))

    return ThreadingHTTPServer((host, port), Handler)


def main():
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    try:
        import uvicorn  # type: ignore
        uvicorn.run(build_fastapi_app(), host=host, port=port, log_level="warning")
    except ImportError:
        httpd = run_stdlib(host, port)
        print(f"[mcp-server] stdlib http.server on {host}:{port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
