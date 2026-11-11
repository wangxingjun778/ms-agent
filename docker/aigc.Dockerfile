# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS dependencies
WORKDIR /build
COPY requirements/aigc-lock.txt ./requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt \
    && /opt/venv/bin/pip check

FROM python:3.12-slim-bookworm AS runtime
ARG GIT_SHA=dev
LABEL org.opencontainers.image.title="MS-Agent AIGC Service" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONPATH=/app \
    AGENTIC_AIGC_DATA_DIR=/data
RUN groupadd --gid 10001 aigc \
    && useradd --uid 10001 --gid 10001 --no-create-home aigc \
    && mkdir -p /app /data \
    && chown 10001:10001 /data
COPY --from=dependencies /opt/venv /opt/venv
WORKDIR /app
COPY ms_agent/ ./ms_agent/
COPY service/ ./service/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"]
ENTRYPOINT ["python", "-m", "service.aigc"]
