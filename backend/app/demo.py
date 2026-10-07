"""Local demo server: offline data, stub LLM, SQLite, one API key (LOCAL-ONLY).

    python -m app.demo                       # http://127.0.0.1:8000, key from CLOUDDIAG_DEMO_KEY
    python -m app.demo --port 8001 --db ../data/demo.db --fresh

Sets offline defaults (unless already set in the environment), migrates the database with
Alembic, generates the offline dataset if missing, and serves the API with uvicorn. Used by the
frontend's end-to-end walk-through and the offline quickstart. Never for real AWS data: it
forces `data_mode=offline` and the stub LLM (D100).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_KEY = "demo-key-change-me"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the API on offline data with the stub LLM.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", type=Path, default=REPO / "data" / "demo" / "demo.db")
    p.add_argument("--fresh", action="store_true", help="delete the demo database first")
    args = p.parse_args(argv)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and args.db.exists():
        args.db.unlink()
    url = f"sqlite+pysqlite:///{args.db.resolve().as_posix()}"
    key = os.environ.get("CLOUDDIAG_DEMO_KEY", DEFAULT_KEY)
    os.environ["CLOUDDIAG_DATABASE_URL"] = url
    os.environ["CLOUDDIAG_DATA_MODE"] = "offline"
    os.environ["CLOUDDIAG_LLM__PROVIDER"] = "stub"
    os.environ["CLOUDDIAG_LLM__MODEL"] = "stub-deterministic"
    os.environ.setdefault("CLOUDDIAG_EMBEDDINGS__PROVIDER", "hashing")
    os.environ.setdefault("CLOUDDIAG_DIAGNOSIS__SELF_CONSISTENCY_SAMPLES", "2")
    os.environ["CLOUDDIAG_API__API_KEYS"] = f'["{key}"]'

    from alembic import command
    from alembic.config import Config

    from app.offline.dataset import DEFAULT_SEED, generate_dataset

    cfg = Config(str(REPO / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "backend" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    dataset = REPO / "data" / "generated" / "synthetic-v1"
    if not (dataset / "manifest.json").is_file():
        generate_dataset(dataset, seed=DEFAULT_SEED)

    import uvicorn

    from app.config import get_settings

    get_settings.cache_clear()
    print(f"demo API on http://{args.host}:{args.port} (offline, stub LLM, smoke-test / synthetic)")
    print("API key: set CLOUDDIAG_DEMO_KEY to choose one (default: the documented demo key)")
    uvicorn.run(
        "app.main:create_app", factory=True, host=args.host, port=args.port, log_level="info"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
