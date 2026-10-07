"""vector store tables (Phase 5)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

from app.db.types import VectorType

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "vector_indexes",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("dimension", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "vector_items",
        sa.Column(
            "index_name", sa.String(128), sa.ForeignKey("vector_indexes.name"), primary_key=True
        ),
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("embedding", VectorType(), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vector_items")
    op.drop_table("vector_indexes")
