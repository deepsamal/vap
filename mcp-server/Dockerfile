# Build from the repo root:  docker build -f mcp-server/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8000 HOST=0.0.0.0
COPY mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp-server/server.py .
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"
CMD ["python", "server.py"]
