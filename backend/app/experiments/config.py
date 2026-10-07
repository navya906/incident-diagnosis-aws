"""Experiment configuration (YAML) and the condition matrix (BRIEF Section 4 + RQ6 + D71)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.contracts.events import EventSource
from app.contracts.evidence import ScoreWeights

CORE_CONDITIONS = ("B1", "B2", "B3", "B4", "Full", "A1", "A2", "A3", "A4", "A5")
KB_CONDITIONS = ("Full-KB-fault-removed", "Full-KB-distractors")
EXTRA_CONDITIONS = ("Full-no-redaction",)
#: Families decide the number of runs and whether self-consistency is sampled (D86).
FAMILIES = ("baseline", "full", "ablation", "sweep")
EVIDENCE_TYPES: dict[str, tuple[EventSource, ...]] = {
    "metrics": (EventSource.CLOUDWATCH_METRIC,),
    "+cloudtrail": (EventSource.CLOUDWATCH_METRIC, EventSource.CLOUDTRAIL),
    "+logs": (EventSource.CLOUDWATCH_METRIC, EventSource.CLOUDTRAIL, EventSource.CLOUDWATCH_LOG),
    "+config": (
        EventSource.CLOUDWATCH_METRIC,
        EventSource.CLOUDTRAIL,
        EventSource.CLOUDWATCH_LOG,
        EventSource.AWS_CONFIG,
    ),
}


class Rq6Config(BaseModel):
    k: list[int] = Field(default_factory=lambda: [5, 10, 20, 40])
    window: list[int] = Field(default_factory=lambda: [5, 15, 30, 60])
    evidence_types: list[str] = Field(default_factory=lambda: list(EVIDENCE_TYPES))

    @field_validator("evidence_types")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(EVIDENCE_TYPES)
        if unknown:
            raise ValueError(f"unknown evidence types {sorted(unknown)}")
        return v


class BootstrapConfig(BaseModel):
    iterations: int = Field(default=2000, ge=100)
    seed: int = 0
    alpha: float = Field(default=0.05, gt=0, lt=1)


class Comparison(BaseModel):
    """A pre-declared comparison: `condition` vs `vs` on `metric` (paired over incidents)."""

    condition: str
    vs: str = "Full"
    metric: str = "root_cause_correct"


#: Pre-declared primary comparisons (D85): each tests one claim of the brief on the primary
#: metric (taxonomy label + resource match). Holm-corrected as one family; everything else in
#: the report is exploratory.
DEFAULT_PRIMARY = [
    Comparison(condition="B2"),  # Full vs LLM + description only
    Comparison(condition="B3"),  # Full vs LLM + raw telemetry (same budget)
    Comparison(condition="B4"),  # graph + RAG on top of ranked evidence
    Comparison(condition="A1"),  # RAG
    Comparison(condition="A2"),  # dependency graph
    Comparison(condition="A5"),  # evidence ranking
]

#: Model families, matched against provider and model name (lower case).
_FAMILY_HINTS = (
    ("openai", ("gpt", "o1", "o3", "o4", "openai")),
    ("google", ("gemini", "gemma", "palm")),
    ("anthropic", ("claude",)),
    ("meta", ("llama",)),
    ("mistral", ("mistral", "mixtral", "codestral")),
    ("alibaba", ("qwen",)),
    ("deepseek", ("deepseek",)),
    ("stub", ("stub",)),
)


def model_family(provider: str, model: str) -> str:
    """Family from the model name first; the provider only as a fallback, because one provider
    (e.g. an OpenAI-compatible gateway) can serve models of many families."""
    for text in (model.lower(), provider.lower()):
        for family, hints in _FAMILY_HINTS:
            if any(h in text for h in hints):
                return family
    return f"unknown:{model.lower().split('-')[0]}"


class JudgeConfig(BaseModel):
    """LLM judge for the free-text rubric (BRIEF Section 3). NOTE: the judge must be a
    DIFFERENT MODEL FAMILY from the diagnosing model (a model grading its own family's output is
    biased towards it); the config is refused otherwise (D88). The judge runs on the exported
    spot-check / results files; its agreement with the human spot check is reported first."""

    enabled: bool = False
    provider: str = "gemini"
    model: str = "gemini-1.5-pro-002"
    note: str = "The judge must be a different model family from the diagnosing model."


class ExperimentConfig(BaseModel):
    """One experiment = one config file + dataset + corpus + code version (-> experiment id)."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    description: str = ""
    label: str = "smoke-test / synthetic"
    split: Literal["dev", "test"] = "dev"
    limit: int | None = Field(default=None, ge=1)
    runs: int = Field(default=1, ge=1)  # N runs for baselines (B1-B4) and Full
    #: Cheaper plan (D86): fewer runs for ablations (A1-A5, KB, no-redaction) and RQ6 sweeps.
    runs_ablations: int | None = Field(default=None, ge=1)  # None = `runs`
    runs_sweeps: int | None = Field(default=None, ge=1)  # None = `runs`
    #: Conditions that sample self-consistency; None = every LLM condition.
    self_consistency_conditions: list[str] | None = None
    primary_comparisons: list[Comparison] = Field(default_factory=lambda: list(DEFAULT_PRIMARY))
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    seed: int = 0
    conditions: list[str] = Field(default_factory=lambda: [*CORE_CONDITIONS, *KB_CONDITIONS])
    rq6: Rq6Config | None = Field(default_factory=Rq6Config)
    self_consistency_samples: int = Field(default=2, ge=0)
    distractors_per_incident: int = Field(default=2, ge=1)
    spot_check_fraction: float = Field(default=0.1, ge=0, le=1)
    bootstrap: BootstrapConfig = Field(default_factory=BootstrapConfig)
    #: Merged into the application Settings (e.g. llm, embeddings, evidence). Never put API keys
    #: here: they come from the environment.
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("conditions")
    @classmethod
    def _known_conditions(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(CORE_CONDITIONS) - set(KB_CONDITIONS) - set(EXTRA_CONDITIONS)
        if unknown:
            raise ValueError(f"unknown conditions {sorted(unknown)}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate conditions")
        return v

    @model_validator(mode="after")
    def _judge_family(self) -> ExperimentConfig:
        llm = self.settings.get("llm", {}) if isinstance(self.settings.get("llm"), dict) else {}
        diag = model_family(llm.get("provider", "stub"), llm.get("model", "stub"))
        judge = model_family(self.judge.provider, self.judge.model)
        if self.judge.enabled and judge == diag:
            raise ValueError(
                f"judge model family ({judge}) must differ from the diagnosing model family"
            )
        return self

    @model_validator(mode="after")
    def _primary_known(self) -> ExperimentConfig:
        known = set(self.conditions) | {"Full"}
        for c in self.primary_comparisons:
            if c.condition not in known or c.vs not in known:
                raise ValueError(f"primary comparison {c.condition} vs {c.vs} is not in the run")
        return self

    def runs_for(self, family: str) -> int:
        if family == "ablation":
            return self.runs_ablations or self.runs
        if family == "sweep":
            return self.runs_sweeps or self.runs
        return self.runs

    def samples_for(self, condition: str) -> int:
        if (
            self.self_consistency_conditions is None
            or condition in self.self_consistency_conditions
        ):
            return self.self_consistency_samples
        return 0

    @model_validator(mode="after")
    def _no_secrets(self) -> ExperimentConfig:
        def walk(obj, path=""):
            if isinstance(obj, dict):
                for k, val in obj.items():
                    if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower():
                        if k.lower() not in ("top_k", "max_tokens", "max_output_tokens"):
                            raise ValueError(f"settings.{path}{k}: secrets come from env only")
                    walk(val, f"{path}{k}.")

        walk(self.settings)
        return self

    @classmethod
    def load(cls, path: str | Path) -> ExperimentConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Condition:
    """How one condition runs. `kind` rules -> B1; llm -> DiagnosisEngine."""

    name: str
    description: str
    kind: Literal["rules", "llm"] = "llm"
    context_mode: str = "ranked"
    use_graph: bool = True
    use_anomalies: bool = True
    use_chains: bool = True
    use_rag: bool = True
    ranking_mode: str = "score"
    zero_weight: str | None = None
    redaction: bool = True  # False only for the "Full-no-redaction" condition
    kb_mode: Literal["standard", "fault_removed", "distractors"] = "standard"
    top_k: int | None = None
    window_minutes: int | None = None
    evidence_types: str | None = None
    family: str = "baseline"
    extra: dict = field(default_factory=dict)

    def weights(self, base: ScoreWeights) -> ScoreWeights | None:
        if self.zero_weight is None:
            return None
        return base.model_copy(update={self.zero_weight: 0.0})


CONDITIONS: dict[str, Condition] = {
    "B1": Condition("B1", "Rule-based diagnosis (rules written on dev scenarios only)", "rules"),
    "B2": Condition("B2", "LLM + description only", context_mode="description", use_rag=False),
    "B3": Condition(
        "B3",
        "LLM + description + raw telemetry, truncated to the same token budget by recency",
        context_mode="raw",
        use_rag=False,
    ),
    "B4": Condition(
        "B4",
        "LLM + description + ranked evidence (temporal, resource, anomaly, semantic; "
        "no graph, no RAG)",
        use_graph=False,
        use_rag=False,
        zero_weight="dependency",
    ),
    "Full": Condition("Full", "B4 + dependency graph + historical RAG", family="full"),
    "A1": Condition("A1", "Full - RAG", use_rag=False, family="ablation"),
    "A2": Condition(
        "A2",
        "Full - dependency graph (weight 0, no graph context)",
        use_graph=False,
        zero_weight="dependency",
        family="ablation",
    ),
    "A3": Condition(
        "A3",
        "Full - anomaly detection (weight 0, no anomaly context)",
        use_anomalies=False,
        zero_weight="anomaly",
        family="ablation",
    ),
    "A4": Condition(
        "A4",
        "Full - temporal correlation (no chains, temporal weight 0)",
        use_chains=False,
        zero_weight="temporal",
        family="ablation",
    ),
    "A5": Condition(
        "A5",
        "Full - evidence ranking (top-K by recency)",
        ranking_mode="recency",
        family="ablation",
    ),
    "Full-KB-fault-removed": Condition(
        "Full-KB-fault-removed",
        "Full with every knowledge-base entry of the incident's true fault type removed (D71)",
        kb_mode="fault_removed",
        family="ablation",
    ),
    "Full-KB-distractors": Condition(
        "Full-KB-distractors",
        "Full with distractor entries (same wording and signals, different root cause) (D71)",
        kb_mode="distractors",
        family="ablation",
    ),
    "Full-no-redaction": Condition(
        "Full-no-redaction",
        "Full with redaction switched off (measures redaction's effect on quality; offline "
        "synthetic data only, refused in aws mode) (D87)",
        redaction=False,
        family="ablation",
    ),
}


def condition_matrix(config: ExperimentConfig) -> list[Condition]:
    """Core and knowledge-base conditions in config order, then the RQ6 sweeps (Full with one
    parameter changed). Full is always included because comparisons are paired against it."""
    names = list(config.conditions)
    if "Full" not in names:
        names.insert(0, "Full")
    out = [CONDITIONS[n] for n in names]
    if config.rq6 is not None:
        for k in config.rq6.k:
            out.append(Condition(f"RQ6-K{k}", f"Full with K={k}", top_k=k, family="sweep"))
        for w in config.rq6.window:
            out.append(
                Condition(
                    f"RQ6-W{w}", f"Full with a {w}-min window", window_minutes=w, family="sweep"
                )
            )
        for t in config.rq6.evidence_types:
            out.append(
                Condition(
                    f"RQ6-E{t}", f"Full with evidence types {t}", evidence_types=t, family="sweep"
                )
            )
    return out
