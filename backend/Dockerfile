FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY backend/pyproject.toml ./backend/pyproject.toml
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.7" \
    pydantic-settings pyyaml "sqlalchemy>=2.0" "alembic>=1.13" "psycopg[binary]>=3.1" \
    numpy pandas scikit-learn networkx

COPY backend ./backend
COPY config ./config
WORKDIR /srv/backend

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
