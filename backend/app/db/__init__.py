from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base

__all__ = ["Base"]
