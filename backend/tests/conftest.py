import os

# Keep tests hermetic: SQLite instead of Postgres.
os.environ.setdefault("CLOUDDIAG_DATABASE_URL", "sqlite+pysqlite:///:memory:")
