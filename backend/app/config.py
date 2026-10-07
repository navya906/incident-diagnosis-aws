"""Settings: YAML defaults (config/default.yaml) overridden by environment variables."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from app.contracts.evidence import ScoreWeights

_REPO_ROOT = Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return Path(os.environ.get("CLOUDDIAG_CONFIG_FILE", _REPO_ROOT / "config" / "default.yaml"))


#: Anomaly-component prior for events that have no numeric anomaly (BRIEF Section 2).
DEFAULT_EVENT_PRIORS: dict[str, float] = {
    "change": 0.7,  # mutating CloudTrail call or AWS Config change (deployments, config edits)
    "api_error": 0.9,  # CloudTrail call that failed (errorCode set, e.g. AccessDenied)
    "read_only": 0.0,  # Describe*/Get*/List* calls
    "error_log": 0.6,
    "warning_log": 0.3,
    "info_log": 0.05,
}


class EvidenceSettings(BaseModel):
    """Evidence ranking: score = weighted sum of five components in [0, 1] (BRIEF Section 2)."""

    top_k: int = Field(default=10, ge=1)
    window_minutes: int = Field(default=30, ge=1)
    #: Investigation windows offered for sweeps (RQ6); `window_minutes` is the one in use.
    window_options: list[int] = Field(default_factory=lambda: [5, 15, 30, 60])
    #: Minutes after the alarm still included (effects and late logs trail the alarm).
    post_alarm_minutes: int = Field(default=10, ge=0)
    #: Tuned on the dev split (DECISIONS D55); `ScoreWeights()` keeps the Phase 0 placeholders.
    weights: ScoreWeights = Field(
        default_factory=lambda: ScoreWeights(
            temporal=0.15, resource=0.0, anomaly=0.3, semantic=0.3, dependency=0.3
        )
    )
    #: Detector used for the anomaly component and onset estimate; threshold None = its default.
    anomaly_method: str = "zscore"
    anomaly_threshold: float | None = 6.0  # dev choice (D55); None = the detector's default
    #: Temporal component: exp decay from the estimated onset (minutes), faster after onset.
    temporal_tau_before_minutes: float = Field(default=5.0, gt=0)
    temporal_tau_after_minutes: float = Field(default=10.0, gt=0)
    #: At most this many items from one group (metric series, log template, API call, resource
    #: config) in the top-K, so one noisy series cannot fill the list. 0 disables the cap.
    max_per_group: int = Field(default=2, ge=0)
    #: Group members within this fraction of the group's best score are eligible; the earliest
    #: eligible members are taken (first occurrences carry the information).
    group_member_ratio: float = Field(default=0.9, gt=0, le=1)
    event_priors: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_EVENT_PRIORS))


class CorrelationSettings(BaseModel):
    """Signals, event chains and candidate causes (temporal precedence + dependency path)."""

    max_link_gap_minutes: float = Field(default=10.0, gt=0)
    max_link_hops: int = Field(default=3, ge=1)
    min_anomaly_run_points: int = Field(default=2, ge=1)
    #: Onset estimate: anomaly runs active at the alarm that started at most this long before it.
    onset_lookback_minutes: float = Field(default=15.0, gt=0)
    max_candidate_causes: int = Field(default=5, ge=1)
    #: Strength of each signal kind when ranking candidate causes.
    signal_strength: dict[str, float] = Field(
        default_factory=lambda: {
            "change": 1.0,
            "api_error": 0.9,
            "anomaly": 0.6,
            "error_log": 0.5,
            "warning_log": 0.3,
        }
    )


class SectionShares(BaseModel):
    """Share of the context token budget per section (normalised; unused budget flows on)."""

    incident: float = Field(default=0.10, ge=0)
    timeline: float = Field(default=0.15, ge=0)
    anomalies: float = Field(default=0.20, ge=0)
    logs: float = Field(default=0.20, ge=0)
    cloudtrail: float = Field(default=0.15, ge=0)
    graph: float = Field(default=0.08, ge=0)
    historical: float = Field(default=0.12, ge=0)


class DiagnosisSettings(BaseModel):
    review_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    #: Extra samples for self-consistency (0 disables sampling; the primary answer is separate).
    self_consistency_samples: int = Field(default=5, ge=0)
    self_consistency_temperature: float = Field(default=0.7, ge=0, le=2)
    #: Approximate tokens (chars / 4) for the context sections; the API reports real counts.
    context_token_budget: int = Field(default=6000, ge=500)
    section_shares: SectionShares = Field(default_factory=SectionShares)
    max_signals_per_section: int = Field(default=25, ge=1)
    #: Relative tolerance when a number quoted in an explanation is matched to its evidence.
    quote_tolerance: float = Field(default=0.01, ge=0)
    #: Reject FACT explanations with no lexical support in the cited line (else only count
    #: them in the hallucination rate). Off by default: paraphrasing models could trip it.
    reject_unsupported_claims: bool = False
    use_historical: bool = True


class CustomRedactionPattern(BaseModel):
    """Extra pattern, e.g. an internal ticket or customer id. Matches become ``<NAME>_n``;
    with a named group ``value`` only that group is replaced (key=value style)."""

    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,30}$")
    pattern: str
    secret: bool = False  # secrets are never restored in model output

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            compiled = re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex {v!r}: {e}") from e
        if compiled.match(""):
            raise ValueError(f"pattern {v!r} matches the empty string")
        return v


#: Categories that must stay on in real-AWS mode (fail closed, DECISIONS D68).
REQUIRED_AWS_CATEGORIES = ("account_ids", "arns", "ips", "principals", "secrets", "hostnames")


class RedactionSettings(BaseModel):
    """Applied before ANY external LLM or embedding call (app/ai/redaction.py)."""

    enabled: bool = True
    account_ids: bool = True
    arns: bool = True
    ips: bool = True
    principals: bool = True  # IAM users/roles/sessions, unique ids, access key ids, emails
    secrets: bool = True  # key=value secrets, bearer tokens, JWTs, private keys, URL passwords
    hostnames: bool = True  # *.amazonaws.com / *.compute.internal endpoints
    resource_names: bool = False  # canonical ids like rds/db-1c1d -> rds/RESOURCE_1
    #: Refuse to send text that still contains any raw value the redactor replaced.
    strict: bool = True
    #: Bare 40-character AWS secret-key-like strings (mixed case + digit, base64 alphabet).
    bare_secret_keys: bool = True
    custom_patterns: list[CustomRedactionPattern] = Field(default_factory=list)


class LLMSettings(BaseModel):
    provider: str = "stub"  # stub (LOCAL-ONLY) | openai_compatible | gemini
    model: str = "stub-deterministic"
    base_url: str | None = None
    api_key: SecretStr | None = None  # env only: CLOUDDIAG_LLM__API_KEY
    temperature: float = Field(default=0.0, ge=0, le=2)  # primary answer
    max_output_tokens: int = Field(default=2048, ge=64)
    seed: int = 0
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    #: Prices for the cost estimate (USD per 1,000 tokens); set them for the model you use.
    input_cost_per_1k: float = Field(default=0.0, ge=0)
    output_cost_per_1k: float = Field(default=0.0, ge=0)


class CriticalityRule(BaseModel):
    """Static business criticality: the first rule whose regex matches a resource id or its
    service wins; the incident takes the highest level among its affected resources."""

    match: str
    level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]

    @field_validator("match")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex {v!r}: {e}") from e
        return v


class SeveritySettings(BaseModel):
    """Deterministic severity engine (the LLM's severity is advisory only)."""

    default_criticality: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    criticality_rules: list[CriticalityRule] = Field(default_factory=list)
    #: Upper bounds for 0, 1, 2 points (>= last bound scores 3).
    resources_bounds: list[float] = Field(default_factory=lambda: [1, 2, 4])
    error_rate_bounds: list[float] = Field(default_factory=lambda: [0.01, 0.05, 0.25])
    duration_minutes_bounds: list[float] = Field(default_factory=lambda: [5, 15, 60])
    availability_loss_bounds: list[float] = Field(default_factory=lambda: [0.001, 0.01, 0.05])
    latency_ratio_bounds: list[float] = Field(default_factory=lambda: [2.0, 5.0])  # 0, 1, else 2
    #: Total points -> level: <= LOW, <= MEDIUM, <= HIGH, else CRITICAL.
    level_bounds: list[int] = Field(default_factory=lambda: [2, 5, 8])
    #: Error rate or availability loss at or above this forces at least HIGH.
    major_outage_fraction: float = Field(default=0.5, gt=0, le=1)


class EmbeddingSettings(BaseModel):
    """One embedding model per vector index (model name and dimension stored with the index)."""

    provider: str = "sentence_transformers"  # sentence_transformers | hashing | openai | gemini
    model: str = "all-MiniLM-L6-v2"
    dimension: int = Field(default=384, ge=8)  # used by `hashing`; others report their own
    base_url: str | None = None  # OpenAI-compatible endpoint (also local servers)
    api_key: SecretStr | None = None  # env only: CLOUDDIAG_EMBEDDINGS__API_KEY
    batch_size: int = Field(default=64, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)


class VectorStoreSettings(BaseModel):
    backend: str = "faiss"  # faiss (numpy exact search when faiss is not installed) | pgvector
    path: str | None = None  # directory for the faiss/numpy store; None = in memory


class RerankWeights(BaseModel):
    similarity: float = Field(default=0.7, ge=0)  # embedding cosine
    signals: float = Field(default=0.2, ge=0)  # Jaccard of metric / API-call / log vocabularies
    context: float = Field(default=0.1, ge=0)  # same alarm metric and resource types


class RagSettings(BaseModel):
    """Historical-incident retrieval. The knowledge base never contains test incidents."""

    enabled: bool = True
    corpus_version: str = "historical-v1"
    corpus_seed: int = 7
    index_name: str = "historical"
    candidates: int = Field(default=20, ge=1)  # nearest neighbours before re-ranking
    top_k: int = Field(default=3, ge=1)
    min_score: float = 0.0
    rerank: RerankWeights = Field(default_factory=RerankWeights)


class ZScoreParams(BaseModel):
    threshold: float = Field(default=3.0, gt=0)


class MadParams(BaseModel):
    threshold: float = Field(default=3.5, gt=0)  # Iglewicz & Hoaglin's recommended cut-off


class MovingAverageParams(BaseModel):
    threshold: float = Field(default=0.5, gt=0)  # relative deviation from the moving average


class RollingStdParams(BaseModel):
    threshold: float = Field(default=3.0, gt=0)  # recent std / baseline std
    recent_points: int = Field(default=3, ge=2)


class IsolationForestParams(BaseModel):
    threshold: float = 0.0  # anomalous when -decision_function > threshold
    train_minutes: int = Field(default=30, ge=1)
    min_train_points: int = Field(default=5, ge=2)
    n_estimators: int = Field(default=100, ge=10)
    random_state: int = 0


class AnomalySettings(BaseModel):
    """Statistical detectors (no LLM). Windows are in minutes so they work at any resolution."""

    methods: list[str] = Field(
        default_factory=lambda: [
            "zscore",
            "mad",
            "moving_average",
            "rolling_std",
            "isolation_forest",
        ]
    )
    baseline_minutes: int = Field(default=30, ge=1)
    min_history_points: int = Field(default=5, ge=2)
    exclude_anomalies_from_baseline: bool = True
    floor_relative: float = Field(default=0.01, ge=0)
    floor_absolute: float = Field(default=1e-6, gt=0)
    zscore: ZScoreParams = Field(default_factory=ZScoreParams)
    mad: MadParams = Field(default_factory=MadParams)
    moving_average: MovingAverageParams = Field(default_factory=MovingAverageParams)
    rolling_std: RollingStdParams = Field(default_factory=RollingStdParams)
    isolation_forest: IsolationForestParams = Field(default_factory=IsolationForestParams)


class AwsSettings(BaseModel):
    """Real-collector settings. Credentials come from the standard AWS chain, never from here."""

    region: str = "us-east-1"
    profile: str | None = None
    max_attempts: int = Field(default=10, ge=1)
    metrics_period_seconds: int | None = None  # None: pick from data age (60/300/3600)
    logs_max_events_per_group: int = Field(default=500, ge=1)
    logs_filter_pattern: str | None = (
        "?ERROR ?Error ?error ?FATAL ?Fatal ?Exception ?WARN ?Warn ?denied ?Denied "
        "?timeout ?Timeout ?refused ?killed"
    )
    logs_max_message_chars: int = Field(default=2000, ge=100)
    cloudtrail_requests_per_second: float = Field(default=2.0, gt=0)
    cloudtrail_ingestion_lag_minutes: int = Field(default=15, ge=0)
    cloudtrail_include_read_only: bool = False
    config_enabled: bool = True
    cache_dir: str | None = None
    cache_ttl_seconds: int = Field(default=3600, ge=0)


class ApiSettings(BaseModel):
    api_keys: list[SecretStr] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLOUDDIAG_", env_nested_delimiter="__", extra="ignore"
    )

    environment: str = "development"
    #: offline = replay/synthetic data; aws = real AWS telemetry. In aws mode the redaction
    #: policy is mandatory and external clients fail closed (DECISIONS D68).
    data_mode: Literal["offline", "aws"] = "offline"
    log_level: str = "INFO"
    log_json: bool = False
    database_url: str = "postgresql+psycopg://clouddiag:clouddiag@localhost:5432/clouddiag"
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    correlation: CorrelationSettings = Field(default_factory=CorrelationSettings)
    diagnosis: DiagnosisSettings = Field(default_factory=DiagnosisSettings)
    severity: SeveritySettings = Field(default_factory=SeveritySettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    rag: RagSettings = Field(default_factory=RagSettings)
    anomaly: AnomalySettings = Field(default_factory=AnomalySettings)
    aws: AwsSettings = Field(default_factory=AwsSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: init > env > .env > YAML file > defaults.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        path = default_config_path()
        if path.is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=path))
        return tuple(sources)


@lru_cache
def get_settings() -> Settings:
    return Settings()
