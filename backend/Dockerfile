FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
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

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
