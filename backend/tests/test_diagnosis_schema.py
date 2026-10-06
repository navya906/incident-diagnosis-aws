import copy

import pytest
from pydantic import ValidationError

from app.contracts import Diagnosis, needs_review

VALID = {
    "incident_summary": "5xx errors on the ALB after a deployment.",
    "root_cause": {
        "taxonomy_label": "deployment_failure",
        "description": "Bad task definition revision rolled out.",
        "confidence": 0.7,
        "resource_id": "ecs-svc-web",
        "label": "INFERENCE",
    },
    "supporting_evidence": [
        {"evidence_id": "ev_1", "explanation": "UpdateService at 12:00", "label": "FACT"}
    ],
    "contradicting_evidence": [],
    "contributing_factors": [{"description": "No canary", "label": "INFERENCE"}],
    "alternative_hypotheses": [
        {
            "taxonomy_label": "cpu_saturation",
            "description": "CPU spike",
            "confidence": 0.2,
            "rejected_because": "CPU stayed under 60%",
            "label": "HYPOTHESIS",
        },
        {
            "taxonomy_label": "other",
            "description": "unknown",
            "confidence": 0.05,
            "rejected_because": "no signal",
            "label": "HYPOTHESIS",
        },
    ],
    "historical_influence": {"used": False, "incident_ids": [], "how": ""},
    "impact_analysis": {
        "services": ["web"],
        "resources": ["ecs-svc-web"],
        "blast_radius": "web tier",
        "user_impact": "errors",
    },
    "severity_suggestion": "HIGH",
    "recommendations": [
        {"category": "IMMEDIATE", "action": "Roll back", "label": "RECOMMENDATION"}
    ],
    "missing_information": [],
    "requires_human_review": False,
}


def make(**overrides):
    data = copy.deepcopy(VALID)
    for path, value in overrides.items():
        node = data
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return data


def test_valid_diagnosis_accepted():
    d = Diagnosis.model_validate(VALID)
    assert d.cited_evidence_ids() == {"ev_1"}


def test_unknown_taxonomy_label_rejected():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(make(root_cause__taxonomy_label="gremlins"))


def test_root_cause_label_must_be_inference():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(make(root_cause__label="FACT"))


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(make(root_cause__confidence=1.2))


def test_confidence_sum_over_one_rejected():
    with pytest.raises(ValidationError, match="sum"):
        Diagnosis.model_validate(make(root_cause__confidence=0.9))


def test_alternatives_must_be_ranked():
    alts = list(reversed(VALID["alternative_hypotheses"]))
    with pytest.raises(ValidationError, match="ranked"):
        Diagnosis.model_validate(make(alternative_hypotheses=alts))


def test_conclusion_without_evidence_rejected():
    with pytest.raises(ValidationError, match="supporting_evidence"):
        Diagnosis.model_validate(make(supporting_evidence=[]))


def test_supporting_evidence_label_must_be_fact_or_inference():
    ev = [{"evidence_id": "ev_1", "explanation": "x", "label": "HYPOTHESIS"}]
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(make(supporting_evidence=ev))


def test_insufficient_evidence_requires_human_review_but_not_evidence():
    data = make(
        root_cause__taxonomy_label="insufficient_evidence",
        supporting_evidence=[],
        alternative_hypotheses=[],
    )
    with pytest.raises(ValidationError, match="human_review"):
        Diagnosis.model_validate(data)
    data["requires_human_review"] = True
    assert Diagnosis.model_validate(data).requires_human_review


def test_historical_influence_consistency():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(make(historical_influence={"used": True, "incident_ids": []}))
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(make(historical_influence={"used": False, "incident_ids": ["h1"]}))


def test_missing_required_field_rejected():
    data = make()
    del data["severity_suggestion"]
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(data)


def test_needs_review_policy():
    d = Diagnosis.model_validate(VALID)
    assert not needs_review(d, 0.6)
    assert needs_review(d, 0.8)
