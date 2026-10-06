"""initial schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # pgvector is used from Phase 5 (VectorStore); enabling it now keeps the DB image ready.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "incidents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scenario_id", sa.String(64)),
        sa.Column("window_start", _TS),
        sa.Column("window_end", _TS),
        sa.Column("affected_resources", sa.JSON, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("extra", sa.JSON, nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id")),
        sa.Column("timestamp", _TS, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(300), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("metric", sa.String(128)),
        sa.Column("value", sa.Float),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("meta", sa.JSON, nullable=False),
        sa.Column("raw_ref", sa.String(512)),
    )
    op.create_index("ix_events_incident_id", "events", ["incident_id"])
    op.create_index("ix_events_timestamp", "events", ["timestamp"])
    op.create_index("ix_events_resource_id", "events", ["resource_id"])
    op.create_table(
        "aws_resources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id")),
        sa.Column("resource_id", sa.String(300), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("region", sa.String(32)),
        sa.Column("attributes", sa.JSON, nullable=False),
    )
    op.create_index("ix_aws_resources_incident_id", "aws_resources", ["incident_id"])
    op.create_index("ix_aws_resources_resource_id", "aws_resources", ["resource_id"])
    op.create_table(
        "resource_relationships",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id")),
        sa.Column("source_id", sa.String(300), nullable=False),
        sa.Column("target_id", sa.String(300), nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
    )
    op.create_index(
        "ix_resource_relationships_incident_id", "resource_relationships", ["incident_id"]
    )
    op.create_index("ix_resource_relationships_source_id", "resource_relationships", ["source_id"])
    op.create_index("ix_resource_relationships_target_id", "resource_relationships", ["target_id"])
    op.create_table(
        "anomalies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("resource_id", sa.String(300), nullable=False),
        sa.Column("metric", sa.String(128), nullable=False),
        sa.Column("timestamp", _TS, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("baseline", sa.Float, nullable=False),
        sa.Column("observed", sa.Float, nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
    )
    op.create_index("ix_anomalies_incident_id", "anomalies", ["incident_id"])
    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(64), primary_key=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("components", sa.JSON, nullable=False),
    )
    op.create_index("ix_evidence_incident_id", "evidence", ["incident_id"])
    op.create_table(
        "diagnoses",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("experiment_id", sa.String(64)),
        sa.Column("condition", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("valid", sa.Boolean, nullable=False),
        sa.Column("output", sa.JSON),
        sa.Column("failure", sa.JSON),
        sa.Column("latency_ms", sa.Float, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
    )
    op.create_index("ix_diagnoses_incident_id", "diagnoses", ["incident_id"])
    op.create_index("ix_diagnoses_experiment_id", "diagnoses", ["experiment_id"])
    op.create_table(
        "historical_incidents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("corpus_version", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("taxonomy_label", sa.String(64), nullable=False),
        sa.Column("resolution", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.String(128)),
        sa.Column("embedding_dim", sa.Integer),
        sa.Column("extra", sa.JSON, nullable=False),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("experiment_id", sa.String(64), primary_key=True),
        sa.Column("split", sa.String(16), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128)),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("finished_at", _TS),
    )
    op.create_table(
        "experiment_results",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "experiment_id",
            sa.String(64),
            sa.ForeignKey("evaluation_runs.experiment_id"),
            nullable=False,
        ),
        sa.Column("incident_id", sa.String(64), nullable=False),
        sa.Column("condition", sa.String(32), nullable=False),
        sa.Column("run_index", sa.Integer, nullable=False),
        sa.Column("retrieved_evidence", sa.JSON, nullable=False),
        sa.Column("diagnosis", sa.JSON),
        sa.Column("ground_truth", sa.JSON, nullable=False),
        sa.Column("metrics", sa.JSON, nullable=False),
    )
    op.create_index("ix_experiment_results_experiment_id", "experiment_results", ["experiment_id"])
    op.create_index("ix_experiment_results_incident_id", "experiment_results", ["incident_id"])


def downgrade() -> None:
    for table in (
        "experiment_results",
        "evaluation_runs",
        "historical_incidents",
        "diagnoses",
        "evidence",
        "anomalies",
        "resource_relationships",
        "aws_resources",
        "events",
        "incidents",
    ):
        op.drop_table(table)
