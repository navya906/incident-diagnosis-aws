"""Documentation gate (BRIEF Phase 10): required documents exist and stay in sync with the code,
and every number the docs cite is traceable to an experiment id or a component report id."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.evaluation import provenance

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"

REQUIRED = {
    "README.md": REPO / "README.md",
    "architecture": DOCS / "architecture.md",
    "API": DOCS / "api.md",
    "DB schema": DOCS / "db-schema.md",
    "AWS setup": DOCS / "aws-setup.md",
    "AI architecture": DOCS / "ai-architecture.md",
    "evaluation and experiment methodology": DOCS / "evaluation.md",
    "security": DOCS / "security.md",
    "deployment": DOCS / "deployment.md",
    "known limitations": DOCS / "known-limitations.md",
    "future work": DOCS / "future-work.md",
    "testing": DOCS / "testing.md",
}


def _published_docs() -> list[Path]:
    """README and docs/*.md written by people (generated experiment output excluded)."""
    return [REPO / "README.md", *sorted(DOCS.glob("*.md")), DOCS / "experiments" / "RUNBOOK.md"]


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_required_document_exists(name):
    path = REQUIRED[name]
    assert path.is_file(), f"missing {name}: {path}"
    assert len(path.read_text(encoding="utf-8")) > 1500, f"{name} is a stub"


@pytest.mark.parametrize(
    ("topic", "patterns"),
    [
        ("synthetic-data validity", [r"synthetic-data validity", r"circularity", r"real AWS"]),
        ("small-sample calibration", [r"small-sample calibration", r"ECE", r"bins?"]),
        ("CloudTrail lag", [r"CloudTrail lag", r"15 minutes", r"ingestion"]),
        ("LLM data exposure", [r"LLM data-exposure risk", r"sent to that provider", r"redaction"]),
    ],
)
def test_known_limitations_state_the_required_points(topic, patterns):
    text = (DOCS / "known-limitations.md").read_text(encoding="utf-8")
    for p in patterns:
        assert re.search(p, text, re.IGNORECASE), f"{topic}: '{p}' not stated"


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def test_api_doc_covers_every_route(tmp_path):
    from app.config import ApiSettings, Settings
    from app.main import create_app

    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'docs.db'}",
        api=ApiSettings(api_keys=["docs-test-key-0123456789"]),
    )
    app = create_app(settings)
    routes = {_norm(path) for path in app.openapi()["paths"] if path.startswith("/api")}
    text = _norm((DOCS / "api.md").read_text(encoding="utf-8"))
    missing = sorted(r for r in routes if f"`{r}`" not in text and f"{r}`" not in text)
    assert len(routes) >= 20 and not missing, f"routes missing from docs/api.md: {missing}"


def test_db_schema_doc_covers_every_table_and_column():
    import app.db.models  # noqa: F401
    from app.db import Base

    text = (DOCS / "db-schema.md").read_text(encoding="utf-8")
    missing = []
    for table in Base.metadata.sorted_tables:
        if f"### {table.name}" not in text:
            missing.append(table.name)
        missing += [f"{table.name}.{c.name}" for c in table.columns if f"`{c.name}`" not in text]
    assert not missing, f"not documented in docs/db-schema.md: {missing}"


LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def test_relative_links_resolve():
    broken = []
    for doc in _published_docs():
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            path = (doc.parent / target.split("#", 1)[0]).resolve()
            if not path.exists():
                broken.append(f"{doc.relative_to(REPO)} -> {target}")
    assert not broken, broken


def test_cited_ids_are_traceable():
    """Every experiment id and report id cited in the docs exists in docs/experiments."""
    runs = DOCS / "experiments" / "runs"
    reports = provenance.reports()
    problems = []
    for doc in [*_published_docs(), REPO / "PROGRESS.md"]:
        text = doc.read_text(encoding="utf-8")
        for exp in set(re.findall(r"\bexp-[0-9a-f]{12}\b", text)):
            if not (runs / exp / "manifest.json").is_file():
                problems.append(f"{doc.name}: {exp} has no run directory")
        for rid in set(re.findall(r"\brpt-[0-9a-f]{12}\b", text)):
            if rid not in reports:
                problems.append(f"{doc.name}: {rid} is not a report in docs/experiments")
    assert not problems, problems


def test_evaluation_doc_cites_an_experiment_and_the_reports():
    text = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    assert re.search(r"\bexp-[0-9a-f]{12}\b", text), "no experiment id in docs/evaluation.md"
    for rid in provenance.reports():
        assert rid in text, f"docs/evaluation.md does not cite {rid}"
    assert "smoke-test / synthetic" in text


def test_component_report_ids_are_consistent():
    assert provenance.check() == []


def test_report_id_is_content_addressed():
    a = provenance.stamp({"meta": {"dataset": "x"}, "rows": [1, 2]})
    b = provenance.stamp({"rows": [1, 2], "meta": {"dataset": "x"}})
    c = provenance.stamp({"meta": {"dataset": "x"}, "rows": [1, 3]})
    assert a["report_id"] == b["report_id"] != c["report_id"]
    assert provenance.report_id(a) == a["report_id"]  # an existing id is not part of the hash
    md = provenance.with_report_id(["# t", "", "- Command: `x`", "rest"], a["report_id"])
    assert md[3].startswith("- Report id:") and a["report_id"] in md[3]
    assert provenance.with_report_id(md, a["report_id"]).count(md[3]) == 1


def test_readme_has_the_offline_quickstart():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docker compose up --build" in text and "demo-key-change-me" in text
    assert "docs/deployment.md" in text
