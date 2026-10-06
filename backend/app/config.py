"""Settings: YAML defaults (config/default.yaml) overridden by environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
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


class EvidenceSettings(BaseModel):
    top_k: int = Field(default=10, ge=1)
    window_minutes: int = Field(default=30, ge=1)
    weights: ScoreWeights = Field(default_factory=ScoreWeights)


class DiagnosisSettings(BaseModel):
    review_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    self_consistency_samples: int = Field(default=5, ge=1)


class RedactionSettings(BaseModel):
    enabled: bool = True


class LLMSettings(BaseModel):
    provider: str = "stub"
    model: str = "stub-deterministic"
    base_url: str | None = None
    api_key: SecretStr | None = None


class EmbeddingSettings(BaseModel):
    provider: str = "sentence_transformers"
    model: str = "all-MiniLM-L6-v2"


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
    log_level: str = "INFO"
    log_json: bool = False
    database_url: str = "postgresql+psycopg://clouddiag:clouddiag@localhost:5432/clouddiag"
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    diagnosis: DiagnosisSettings = Field(default_factory=DiagnosisSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
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
