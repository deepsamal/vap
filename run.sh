#!/usr/bin/env bash
# Convenience entrypoint for the VAP harness.
#   ./run.sh test     # E2E suite without docker (stdlib subprocesses)
#   ./run.sh agent    # start server+proxy locally and run the agent workflows
#   ./run.sh compose  # docker compose up --build
#   ./run.sh validate # docker compose config
set -euo pipefail
cd "$(dirname "$0")"
cmd="${1:-test}"
case "$cmd" in
  test)
    if command -v pytest >/dev/null 2>&1; then
      echo "[run] pytest tests/test_e2e.py"; pytest -q tests/test_e2e.py
    else
      echo "[run] pytest not found; using stdlib runner"; python3 tests/test_e2e.py
    fi ;;
  agent)
    SP=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()")
    PP=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()")
    PORT=$SP HOST=127.0.0.1 python3 mcp-server/server.py & SRV=$!
    sleep 1
    PORT=$PP HOST=127.0.0.1 UPSTREAM_URL="http://127.0.0.1:$SP/mcp" \
      VAP_JUDGE=mock VAP_CONFIG=proxy/vap-gateway.yaml PYTHONPATH=proxy \
      python3 proxy/server.py & PX=$!
    sleep 2
    PROXY_URL="http://127.0.0.1:$PP" python3 agent/run_agent.py || true
    kill "$SRV" "$PX" 2>/dev/null || true ;;
  compose) docker compose up --build ;;
  validate) docker compose config ;;
  *) echo "usage: $0 {test|agent|compose|validate}" >&2; exit 2 ;;
esac
