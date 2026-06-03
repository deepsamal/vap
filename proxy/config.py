"""Config loader for the VAP proxy.

Loads vap-gateway.yaml. Uses PyYAML when available (Docker image); otherwise a tiny
stdlib parser handles the simple nested mapping used here, so no PyPI dependency is
needed for the in-sandbox test path. Env vars still override knobs in EngineConfig.
"""

from __future__ import annotations

import os
from typing import Any, Dict


def _coerce(v: str) -> Any:
    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "~", ""):
        return None
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v.strip('"').strip("'")


def _tiny_yaml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        key, val = key.strip(), val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            parent[key] = [_coerce(x) for x in inner.split(",")] if inner else []
        else:
            parent[key] = _coerce(val)
    return root


def load_config(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path) as fh:
        text = fh.read()
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _tiny_yaml(text)
