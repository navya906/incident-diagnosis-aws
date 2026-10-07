FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

# Dependencies come from backend/pyproject.toml only (DECISIONS D14). The metadata is installed
# first so this layer is cached until the dependency list changes.
COPY backend/pyproject.toml ./backend/pyproject.toml
RUN mkdir -p backend/app && touch backend/app/__init__.py \
    && pip install --no-cache-dir "./backend[rag]" \
    && pip uninstall -y cloud-incident-diagnosis

COPY backend ./backend
COPY config ./config
RUN pip install --no-cache-dir --no-deps ./backend
WORKDIR /srv/backend

# Offline quickstart (D108): the synthetic dataset and the separate historical corpus are
# generated at build time (both deterministic), so the image works with no AWS credentials
# and no network access at run time. smoke-test / synthetic.
RUN python -m app.offline.generate && python -m app.rag.corpus

# Run as an unprivileged user; only /srv/data (generated data, demo files) is writable.
RUN useradd --uid 10001 --no-create-home --home-dir /srv --shell /usr/sbin/nologin app \
    && chown -R app:app /srv/data
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health')"
# One process: diagnosis jobs and rate limits are in-process (DECISIONS D97). --proxy-headers
# takes the client address from X-Forwarded-For only for peers in FORWARDED_ALLOW_IPS
# (uvicorn default 127.0.0.1; the production compose file trusts the nginx container).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --no-server-header"]
