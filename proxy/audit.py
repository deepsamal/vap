"""Append-only, signed, hash-chained audit log.

Every verification decision (session init, per-call, amendment, result binding) is
one JSONL line. Each record is HMAC-signed (sufficient for the harness; swap for a
real KMS / asymmetric key in production) and chained via prev_hash so the log is
tamper-evident. GET /audit returns the records for tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional


class AuditLog:
    def __init__(self, path: Optional[str] = None, secret: Optional[str] = None):
        self.path = path or os.getenv("VAP_AUDIT_PATH", "vap-audit.jsonl")
        self.secret = (secret or os.getenv("VAP_HMAC_SECRET", "vap-dev-secret")).encode()
        self._lock = threading.Lock()
        self._records: List[Dict[str, Any]] = []
        self._prev_hash = "0" * 64
        try:
            open(self.path, "w").close()  # fresh file per process -> isolated runs
        except OSError:
            pass

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def result_hash(result: Any) -> str:
        raw = json.dumps(result, sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    def record(self, *, session_id, kind, commitment_digest, principal, intent, call,
               verdict, verification, result=None, extra=None) -> str:
        with self._lock:
            body = {
                "session_id": session_id, "kind": kind,
                "commitment_digest": commitment_digest, "principal": principal,
                "intent": intent, "call": call, "verdict": verdict,
                "verification": verification,
                "result_hash": self.result_hash(result) if result is not None else None,
                "ts": time.time(), "prev_hash": self._prev_hash,
            }
            if extra:
                body.update(extra)
            canonical = json.dumps(body, sort_keys=True, default=str)
            sig = self._sign(canonical)
            this_hash = hashlib.sha256((self._prev_hash + sig).encode()).hexdigest()
            audit_ref = "audit-" + this_hash[:16]
            record = {"audit_ref": audit_ref, **body,
                      "signature": {"alg": "HMAC-SHA256", "value": sig},
                      "record_hash": this_hash}
            self._prev_hash = this_hash
            self._records.append(record)
            try:
                with open(self.path, "a") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
            except OSError:
                pass
            return audit_ref

    def all(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if session_id is None:
                return list(self._records)
            return [r for r in self._records if r.get("session_id") == session_id]

    def verify_chain(self) -> bool:
        prev = "0" * 64
        for r in self._records:
            body = {k: r[k] for k in r if k not in ("audit_ref", "signature", "record_hash")}
            canonical = json.dumps(body, sort_keys=True, default=str)
            if self._sign(canonical) != r["signature"]["value"]:
                return False
            expect = hashlib.sha256((prev + r["signature"]["value"]).encode()).hexdigest()
            if expect != r["record_hash"]:
                return False
            prev = r["record_hash"]
        return True
