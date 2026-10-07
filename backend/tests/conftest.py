import os

import pytest

# Keep tests hermetic: SQLite instead of Postgres.
os.environ.setdefault("CLOUDDIAG_DATABASE_URL", "sqlite+pysqlite:///:memory:")

# Test categories (docs/testing.md, DECISIONS D109). An explicit marker on the test or its module
# wins; otherwise a test that uses one of these fixtures (generated dataset files, the API client,
# mocked AWS, the full pipeline) is an integration test, and every other test is a unit test.
CATEGORIES = ("unit", "integration", "e2e")
INTEGRATION_FIXTURES = frozenset(
    {
        "dataset",
        "dataset_dir",
        "loader",
        "all_cases",
        "client",
        "aws",
        "aws_mode",
        "world",
        "small_run",
        "retrieval_setup",
    }
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        explicit = [m.name for m in item.iter_markers() if m.name in CATEGORIES]
        if len(set(explicit)) > 1:
            raise pytest.UsageError(f"{item.nodeid}: more than one test category {explicit}")
        if not explicit:
            used = INTEGRATION_FIXTURES & set(getattr(item, "fixturenames", ()))
            item.add_marker("integration" if used else "unit")


def pytest_collection_finish(session):
    counts = dict.fromkeys(CATEGORIES, 0)
    for item in session.items:
        for name in CATEGORIES:
            if item.get_closest_marker(name):
                counts[name] += 1
    session.config._test_categories = counts


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    counts = getattr(config, "_test_categories", None)
    if counts is not None:
        terminalreporter.write_line(
            "test categories (selected): " + ", ".join(f"{k} {v}" for k, v in counts.items())
        )
