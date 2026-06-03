# Build from the repo root:  docker build -f proxy/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=9000 HOST=0.0.0.0 \
    VAP_JUDGE=mock VAP_AUDIT_PATH=/app/vap-audit.jsonl VAP_CONFIG=/app/vap-gateway.yaml
COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy/*.py ./
COPY proxy/vap-gateway.yaml ./
COPY schemas ./schemas
EXPOSE 9000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=2).status==200 else 1)"
CMD ["python", "server.py"]
