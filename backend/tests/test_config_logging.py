import logging

from app.config import Settings
from app.logging_config import SecretFilter, scrub


def test_defaults_load_from_yaml():
    s = Settings()
    assert s.evidence.top_k == 10
    assert s.evidence.weights.temporal == 0.25
    assert s.llm.provider == "stub"


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("CLOUDDIAG_EVIDENCE__TOP_K", "20")
    monkeypatch.setenv("CLOUDDIAG_EVIDENCE__WEIGHTS__TEMPORAL", "0")
    s = Settings()
    assert s.evidence.top_k == 20
    assert s.evidence.weights.temporal == 0


def test_missing_config_file_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDDIAG_CONFIG_FILE", str(tmp_path / "nope.yaml"))
    assert Settings().diagnosis.review_confidence_threshold == 0.6


def test_api_key_not_exposed_in_repr(monkeypatch):
    monkeypatch.setenv("CLOUDDIAG_LLM__API_KEY", "sk-supersecret")
    assert "sk-supersecret" not in repr(Settings())


def test_scrub_removes_secrets():
    out = scrub("login api_key=abc123 token: xyz AKIAIOSFODNN7EXAMPLE")
    assert "abc123" not in out and "xyz" not in out and "AKIAIOSFODNN7EXAMPLE" not in out


def test_log_filter_scrubs_records():
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "password=%s", ("hunter2",), None)
    SecretFilter().filter(rec)
    assert "hunter2" not in rec.getMessage()
