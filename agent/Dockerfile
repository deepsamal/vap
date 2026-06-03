# Build from the repo root:  docker build -f agent/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PROXY_URL=http://vap-proxy:9000
# the agent drives the proxy via the Python reference client (stdlib only)
COPY agent/ ./agent/
COPY clients/python/ ./clients/python/
CMD ["python", "agent/run_agent.py"]
