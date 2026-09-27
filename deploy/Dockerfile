# Home Network BRAIN Starter — hub image.
#
# Base image pinned by digest (python:3.13-slim-trixie, multi-arch index).
# Dependencies installed from requirements.lock with --require-hashes and
# binary wheels only: a changed file on the index fails the build.
# Update the digest deliberately: docker buildx imagetools inspect python:3.13-slim-trixie
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="Home Network BRAIN Starter" \
      org.opencontainers.image.description="Self-hosted, provider-independent home hub"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HNB_DATA_DIR=/var/lib/hnb/data \
    HNB_BACKUP_DIR=/var/lib/hnb/backups \
    HNB_LOG_DIR=/var/lib/hnb/logs \
    HNB_VAULT_KEY_FILE=/run/secrets/vault_key
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY hnb /app/hnb
COPY web /app/web
COPY plugins /app/plugins
# Fallback non-root user; compose runs the container as the host's `hnb` uid:gid.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin hnb
USER 10001:10001
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "hnb.cli", "healthcheck"]
ENTRYPOINT ["python", "-m", "hnb.cli"]
CMD ["serve"]
