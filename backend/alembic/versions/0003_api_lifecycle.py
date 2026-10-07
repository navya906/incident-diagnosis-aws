"""incident lifecycle, diagnosis jobs, audit log (Phase 8)

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("incidents") as b:
        b.add_column(sa.Column("alarm_time", _TS))
        b.add_column(sa.Column("onset_at", _TS))
        b.add_column(sa.Column("updated_at", _TS))
        b.add_column(sa.Column("severity", sa.String(16)))
        b.add_column(sa.Column("source", sa.String(32), nullable=False, server_default="api"))
        b.add_column(sa.Column("dedup_key", sa.String(300)))
        b.create_index("ix_incidents_dedup_key", ["dedup_key"])
    op.create_table(
        "incident_transitions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("at", _TS, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("note", sa.Text, nullable=False),
    )
    op.create_index("ix_incident_transitions_incident_id", "incident_transitions", ["incident_id"])
    op.create_table(
        "diagnosis_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("started_at", _TS),
        sa.Column("finished_at", _TS),
        sa.Column("diagnosis_id", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("requested_by", sa.String(64), nullable=False),
    )
    op.create_index("ix_diagnosis_jobs_incident_id", "diagnosis_jobs", ["incident_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("at", _TS, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.String(64)),
        sa.Column("detail", sa.JSON, nullable=False),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_incident_id", "audit_log", ["incident_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("diagnosis_jobs")
    op.drop_table("incident_transitions")
    with op.batch_alter_table("incidents") as b:
        b.drop_index("ix_incidents_dedup_key")
        for col in ("dedup_key", "source", "severity", "updated_at", "onset_at", "alarm_time"):
            b.drop_column(col)
