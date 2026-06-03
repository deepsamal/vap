"""Cheap, deterministic risk scorer (VAP check C5's gate).

Produces a score in [0,1] from several signals. The proxy fires the (expensive)
semantic judge only when score >= invoke_at OR sensitivity is high. Keeping this
deterministic -- including a SEEDED random-sample component -- makes the E2E tests
stable.

Signals & the semantic_trigger they map to (schema enum):
  irreversibility -> (tier of tool)            scope_boundary-ish, reported as the tool tier
  sensitivity     -> "sensitivity"
  burn_rate       -> "burn_rate_anomaly"
  looping         -> "looping"
  novelty         -> "novelty"
  random_sample   -> "random_sample"
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# per-effect irreversibility tiers (higher = more dangerous)
TOOL_TIER = {"read": 0.0, "writes_data": 0.35, "writes_money": 0.7, "deletes": 1.0}

# intent.sensitivity enum -> weight
SENSITIVITY_WEIGHT = {
    "reads": 0.0, "writes_data": 0.3, "writes_money": 0.7,
    "deletes": 0.9, "sends_external": 0.6, "grants_access": 0.8, None: 0.1,
}

# signal name -> schema semantic_trigger value
TRIGGER_MAP = {
    "irreversibility": "scope_boundary",
    "sensitivity": "sensitivity",
    "burn_rate": "burn_rate_anomaly",
    "looping": "looping",
    "novelty": "novelty",
    "random_sample": "random_sample",
}


@dataclass
class RiskConfig:
    random_sample_rate: float = 0.0
    seed: int = 1337
    w_irreversibility: float = 0.35
    w_sensitivity: float = 0.25
    w_burn: float = 0.15
    w_loop: float = 0.3
    w_novelty: float = 0.1


@dataclass
class RiskResult:
    score: float
    signals: Dict[str, float] = field(default_factory=dict)
    trigger: Optional[str] = None   # schema semantic_trigger value


def _arg_shape(arguments: Dict[str, Any]) -> str:
    items = sorted((k, str(v)) for k, v in (arguments or {}).items())
    raw = ";".join(f"{k}={v}" for k, v in items)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class RiskScorer:
    def __init__(self, config: Optional[RiskConfig] = None,
                 tool_meta: Optional[Dict[str, str]] = None):
        self.cfg = config or RiskConfig()
        self.tool_meta = tool_meta or {}
        self._rng = random.Random(self.cfg.seed)

    def call_key(self, tool: str, arguments: Dict[str, Any]) -> str:
        return f"{tool}:{_arg_shape(arguments)}"

    def score(self, tool, arguments, sensitivity, *, seen_calls, used_tools,
              calls_made, cost_spent, max_cost, max_calls):
        cfg = self.cfg
        signals: Dict[str, float] = {}
        contributors = []

        effect = self.tool_meta.get(tool, "read")
        irr = TOOL_TIER.get(effect, 0.2)
        signals["irreversibility"] = irr
        contributors.append((irr * cfg.w_irreversibility, "irreversibility"))

        sens = SENSITIVITY_WEIGHT.get(sensitivity, 0.1)
        signals["sensitivity"] = sens
        contributors.append((sens * cfg.w_sensitivity, "sensitivity"))

        burn = 0.0
        if max_cost and max_calls and max_calls > 0:
            cost_frac = cost_spent / max_cost if max_cost else 0.0
            call_frac = calls_made / max_calls
            burn = max(0.0, cost_frac - call_frac)
        signals["burn_rate"] = round(burn, 4)
        contributors.append((min(burn, 1.0) * cfg.w_burn, "burn_rate"))

        key = self.call_key(tool, arguments)
        repeats = seen_calls.get(key, 0)
        loop = min(repeats / 2.0, 1.0)
        signals["looping"] = round(loop, 4)
        contributors.append((loop * cfg.w_loop, "looping"))

        novel = 0.0 if tool in used_tools else 1.0
        signals["novelty"] = novel
        contributors.append((novel * cfg.w_novelty, "novelty"))

        sampled = self._rng.random() < cfg.random_sample_rate
        signals["random_sample"] = 1.0 if sampled else 0.0
        if sampled:
            contributors.append((1.0, "random_sample"))

        raw = sum(c for c, _ in contributors)
        final = max(0.0, min(raw, 1.0))

        contributors.sort(reverse=True)
        top = contributors[0][1] if contributors and contributors[0][0] > 0 else None
        trigger = TRIGGER_MAP.get(top) if top else None
        return RiskResult(score=final, signals=signals, trigger=trigger)
