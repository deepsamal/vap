"""Pluggable semantic judge.

The judge answers one question: given the session goal and this call's stated
rationale/expected_effect, is the actual tool call consistent? It is the only
place where (in production) an LLM is invoked. VAP gates it behind a cheap
deterministic risk scorer so the expensive judge fires rarely.

Two implementations:
* MockSemanticJudge -- deterministic, rule-based. Used in tests so E2E is
  reproducible with no network. Reports 0 tokens.
* LLMSemanticJudge  -- a real, configurable OpenAI-compatible client. Posts to
  ``POST {base_url}/chat/completions`` (OpenAI / Ollama / vLLM / LM Studio / ...),
  asks for a strict-JSON verdict, and reports the API's reported token usage so
  the proxy can bill verification cost to the session ``tokens`` meter. Never
  called by the default (mock) test path.

Select via env VAP_JUDGE=mock|llm (default mock). The LLM judge is configured via
env vars (and mirrored in vap-gateway.yaml):
    VAP_LLM_BASE_URL   e.g. https://api.openai.com/v1 or http://localhost:11434/v1
    VAP_LLM_API_KEY    optional; omit/blank for local Ollama
    VAP_LLM_MODEL      e.g. gpt-4o-mini, llama3.1
    VAP_LLM_TIMEOUT    seconds (default 20)
    VAP_LLM_TEMPERATURE float (default 0)
    VAP_LLM_FAILOPEN   "true" to treat a broken judge as consistent (default false
                       = fail safe: a broken judge does NOT rubber-stamp).

Interface:
    evaluate(goal, rationale, expected_effect, call)
        -> (consistent: bool, confidence: float, reason: str, tokens: int)

``tokens`` is the verification's token usage (0 for the mock judge and for any
path that does not consult an LLM).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, Optional, Tuple

# (consistent, confidence, reason, tokens_used)
JudgeResult = Tuple[bool, float, str, int]


class SemanticJudge:
    def evaluate(self, goal: str, rationale: str, expected_effect: Optional[str],
                 call: Dict[str, Any]) -> JudgeResult:
        raise NotImplementedError


def _ctx(*texts: Optional[str]) -> str:
    return " ".join(t for t in texts if t).lower()


def _mentions(haystack: str, needle: Optional[str]) -> bool:
    return bool(needle) and str(needle).lower() in haystack


_TICKET_RE = re.compile(r"ticket|tck[-_ ]?\d+|sup[-_ ]?\d+|#\d+|case[-_ ]?\d+", re.I)


class MockSemanticJudge(SemanticJudge):
    """Deterministic, rule-based judge (no network, 0 tokens).

    Rules:
    * payments.refund -- the call's order_id MUST appear in rationale/expected_effect
      AND a ticket/case reference must be present. Missing order linkage =>
      high-confidence inconsistent (the "in-scope-but-incoherent" case). Missing
      ticket only => low-confidence inconsistent (ask to clarify).
    * admin.delete_user -- inconsistent unless the goal explicitly mentions
      deletion/offboarding.
    * otherwise -- consistent if rationale shares signal with the goal, else
      low-confidence consistent.
    """

    def evaluate(self, goal, rationale, expected_effect, call):
        tool = call.get("tool", "")
        args = call.get("arguments", {}) or {}
        ctx = _ctx(rationale, expected_effect, goal)

        if tool == "payments.refund":
            order_id = args.get("order_id")
            order_linked = _mentions(ctx, order_id)
            has_ticket = bool(_TICKET_RE.search(ctx))
            if not order_linked:
                return (False, 0.92,
                        f"refund targets order_id={order_id} but no rationale/"
                        "expected_effect references that order -- intent not grounded", 0)
            if not has_ticket:
                return (False, 0.6,
                        "refund references the order but cites no support ticket/case; "
                        "needs human confirmation", 0)
            return (True, 0.9,
                    f"refund order {order_id} is referenced and a ticket is cited; "
                    "consistent with goal", 0)

        if tool == "admin.delete_user":
            if re.search(r"delete|offboard|remov|terminat|gdpr|erasure", goal, re.I):
                return (True, 0.8, "destructive action aligns with stated deletion goal", 0)
            return (False, 0.95,
                    "user-deletion is not implied by the session goal; high-risk and "
                    "off-mission", 0)

        goal_words = set(re.findall(r"[a-z]{4,}", goal.lower()))
        rat_words = set(re.findall(r"[a-z]{4,}", _ctx(rationale, expected_effect)))
        overlap = goal_words & rat_words
        if overlap:
            return (True, 0.7,
                    "rationale overlaps goal on: " + ", ".join(sorted(overlap)[:5]), 0)
        return (True, 0.4, "no strong signal either way; allowing with low confidence", 0)


_SYS_PROMPT = (
    "You are a VAP semantic verifier. Decide whether a single tool call is "
    "consistent with the agent's stated session goal, its per-call rationale, and "
    "its declared expected_effect. Be skeptical of money-moving or destructive "
    "calls that are not clearly grounded in the goal/rationale. Reply with ONLY a "
    'strict JSON object: {"consistent": <true|false>, "confidence": <0..1>, '
    '"reason": "<short explanation>"}. No prose outside the JSON.')


def _build_user_prompt(goal, rationale, expected_effect, call) -> str:
    return (f"GOAL:\n{goal}\n\n"
            f"RATIONALE:\n{rationale}\n\n"
            f"EXPECTED_EFFECT:\n{expected_effect}\n\n"
            f"CALL:\n{json.dumps(call, sort_keys=True)}\n\n"
            "Is the call consistent with the goal + rationale + expected_effect?")


def _extract_json(content: str) -> Dict[str, Any]:
    """Parse the model's reply. Tries strict json first, then defensively pulls the
    first {...} block out of surrounding prose / code fences."""
    content = (content or "").strip()
    try:
        return json.loads(content)
    except Exception:
        pass
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"no JSON object found in judge reply: {content[:200]!r}")


class LLMSemanticJudge(SemanticJudge):
    """Real OpenAI-compatible LLM judge (Chat Completions shape).

    Works against OpenAI, Ollama (http://localhost:11434/v1), vLLM, LM Studio, etc.
    Uses httpx when available (the FastAPI/Docker path) and falls back to stdlib
    urllib so it still runs with zero PyPI deps. Reports token usage from the API
    response (usage.total_tokens) so the proxy bills verification to the token meter.

    Robustness: on network error/timeout/malformed reply it FAILS SAFE per
    VAP_LLM_FAILOPEN (default false): treat as NOT consistent so a broken judge
    escalates to clarify rather than rubber-stamping. Set VAP_LLM_FAILOPEN=true to
    fail open instead.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[float] = None,
                 temperature: Optional[float] = None,
                 fail_open: Optional[bool] = None) -> None:
        self.base_url = (base_url if base_url is not None
                         else os.getenv("VAP_LLM_BASE_URL",
                                        "http://localhost:11434/v1")).rstrip("/")
        self.api_key = (api_key if api_key is not None
                        else os.getenv("VAP_LLM_API_KEY", "")) or ""
        self.model = model or os.getenv("VAP_LLM_MODEL", "gpt-4o-mini")
        self.timeout = float(timeout if timeout is not None
                             else os.getenv("VAP_LLM_TIMEOUT", "20"))
        self.temperature = float(temperature if temperature is not None
                                 else os.getenv("VAP_LLM_TEMPERATURE", "0"))
        if fail_open is None:
            fail_open = os.getenv("VAP_LLM_FAILOPEN", "false").lower() in ("1", "true", "yes")
        self.fail_open = bool(fail_open)

    # -- HTTP --------------------------------------------------------------- #
    def _request_body(self, goal, rationale, expected_effect, call) -> Dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            # response_format is honored by OpenAI/vLLM/LM Studio; harmlessly ignored
            # by backends that don't support it (we also parse defensively below).
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYS_PROMPT},
                {"role": "user",
                 "content": _build_user_prompt(goal, rationale, expected_effect, call)},
            ],
        }

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        payload = json.dumps(body).encode()
        try:
            import httpx  # type: ignore
            resp = httpx.post(url, headers=self._headers(), content=payload,
                              timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except ImportError:
            import urllib.request
            req = urllib.request.Request(url, data=payload, headers=self._headers(),
                                         method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())

    # -- API ---------------------------------------------------------------- #
    def evaluate(self, goal, rationale, expected_effect, call) -> JudgeResult:
        body = self._request_body(goal, rationale, expected_effect, call)
        try:
            data = self._post(body)
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            tokens = int(usage.get("total_tokens")
                         or (int(usage.get("prompt_tokens", 0))
                             + int(usage.get("completion_tokens", 0))))
            verdict = _extract_json(content)
            consistent = bool(verdict["consistent"])
            confidence = float(verdict.get("confidence", 0.5))
            reason = str(verdict.get("reason", ""))
            return (consistent, confidence, reason, tokens)
        except Exception as exc:  # network error / timeout / malformed reply
            print(f"[vap-judge] LLM judge error: {exc!r}; "
                  f"fail_open={self.fail_open}", file=sys.stderr, flush=True)
            if self.fail_open:
                return (True, 0.0,
                        f"LLM judge unavailable ({exc}); failing OPEN per config", 0)
            return (False, 0.0,
                    f"LLM judge unavailable ({exc}); failing SAFE (treat as "
                    "inconsistent -> clarify)", 0)


def get_judge(name: Optional[str] = None) -> SemanticJudge:
    name = (name or os.getenv("VAP_JUDGE", "mock")).lower()
    return LLMSemanticJudge() if name == "llm" else MockSemanticJudge()
