# LLM Gateway — production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install the gateway package (dependencies + gateway/ itself)
COPY pyproject.toml README.md ./
COPY gateway ./gateway
RUN pip install --no-cache-dir .

# Default runtime configuration (override by mounting your own config.yaml)
COPY config.yaml ./config.yaml

# Run as a non-root user; /data is the volume mount point for the SQLite
# database (GATEWAY_DATABASE_URL=sqlite+aiosqlite:////data/gateway.db) and
# /app/backups is where POST /admin/backup writes snapshots.
RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /data /app/backups \
    && chown -R gateway:gateway /data /app/backups
USER gateway

EXPOSE 8000

# Liveness via the readiness endpoint (curl is not in slim; the stdlib is).
# /readyz covers startup-complete + DB reachable + not-draining, which is
# what a container orchestrator should act on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)" || exit 1

# --timeout-graceful-shutdown mirrors the in-app drain: uvicorn stops
# accepting, in-flight requests finish and background work (log queue,
# spend accounting, shadow mirrors) is flushed within the window.
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30s"]
