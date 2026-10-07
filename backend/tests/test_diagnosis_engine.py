"""Phase 6: LLM clients, prompts, context builder, validator + repair, citations,
self-consistency and the end-to-end pipeline with the stub LLM."""

import json
import re
import urllib.error

import pytest

from app.ai.clients import RedactionPolicyError, create_llm_client
from app.ai.llm_clients import (
    GeminiClient,
    LLMCallError,
    OpenAICompatibleClient,
    StubLLM,
    build_llm_client,
)
from app.ai.redaction import RedactingLLMClient
from app.config import (
    DiagnosisSettings,
    EmbeddingSettings,
    LLMSettings,
    RedactionSettings,
    SectionShares,
    Settings,
)
from app.contracts.evidence import make_evidence_id
from app.diagnosis.context import EVIDENCE_LINE, SECTION_ORDER, ContextBuilder, estimate_tokens
from app.diagnosis.engine import DiagnosisEngine
from app.diagnosis.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_repair_request,
    build_request,
    prompt_sha,
)
from app.diagnosis.validator import parse_json, validate_output
from app.evaluation import diagnosis_e2e
from app.evidence.pipeline import InvestigationPipeline
from app.interfaces._guard import DirectConstructionError
from app.interfaces.llm_client import LLMClient, LLMRequest, LLMResponse
from app.offline.dataset import DatasetLoader, generate_dataset
from app.offline.replay import ReplayCollector
from app.rag.corpus import build_corpus, heldout_fingerprints
from app.rag.embedders import HashingEmbedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase, build_query
from app.rag.vector_store import LocalVectorStore


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("phase6-ds")
    generate_dataset(out, seed=42)
    return out


@pytest.fixture(scope="module")
def world(dataset, tmp_path_factory):
    loader = DatasetLoader(dataset)
    collector = ReplayCollector(loader)
    kb = KnowledgeBase(HashingEmbedder(256), LocalVectorStore(), "h", heldout_fingerprints(loader))
    kb.build(build_corpus(tmp_path_factory.mktemp("hist")))
    retriever = HistoricalRetriever(kb)
    settings = Settings()
    pipeline = InvestigationPipeline(settings)
    ids = [
        i
        for i in loader.incident_ids("dev")
        if loader.load_truth(i).ground_truth.evidence_event_ids
    ]
    return loader, collector, retriever, settings, pipeline, ids


def _case(world, idx=0, budget=None, include_graph=True, with_history=True):
    loader, collector, retriever, settings, pipeline, ids = world
    iid = ids[idx]
    s = loader.load(iid)
    inv = pipeline.run_offline(collector, iid)
    retrieved = None
    if with_history:
        q = build_query(s.incident, s.events, [i.event_id for i in inv.ranking.items], s.resources)
        retrieved = retriever.retrieve(q)
    ds = DiagnosisSettings() if budget is None else DiagnosisSettings(context_token_budget=budget)
    ctx = ContextBuilder(ds).build(
        incident=s.incident,
        events=s.events,
        investigation=inv,
        retrieved=retrieved,
        include_graph=include_graph,
        include_historical=retrieved is not None,
    )
    return s, inv, retrieved, ctx


# ------------------------------------------------------------------ context builder
def test_context_sections_are_ordered_and_every_fact_line_is_citable(world):
    ids = world[-1]
    idx = next(
        i
        for i in range(len(ids))
        if {"logs", "cloudtrail"} <= set(_case(world, i)[3].section_names())
    )
    s, inv, retrieved, ctx = _case(world, idx)
    names = ctx.section_names()
    assert names == [n for n in SECTION_ORDER if n in names]
    assert names[0] == "incident" and names[-1] == "historical"
    assert {"timeline", "anomalies", "logs", "cloudtrail", "graph"} <= set(names)
    headers = [line for line in ctx.text.splitlines() if line.startswith("## ")]
    assert headers == [f"## {n.upper()}" for n in names]
    lines = [m for line in ctx.text.splitlines() if (m := EVIDENCE_LINE.match(line))]
    assert lines
    for m in lines:
        ref = ctx.evidence_index[m.group("evd")]
        assert ref.evidence_id == make_evidence_id(s.incident.incident_id, ref.event_id)
        assert ref.line == m.group(0)
    for evd in re.findall(r"\[(evd_[0-9a-f]{16})\]", ctx.text):
        assert evd in ctx.evidence_index  # chain references are citable too
    assert "Description (symptoms only)" in ctx.text
    assert "NOT evidence" in ctx.text and ctx.retrieved_ids == [r.incident_id for r in retrieved]


def test_context_respects_the_token_budget_and_counts_dropped_items(world):
    _, _, _, full = _case(world)
    _, _, _, tight = _case(world, budget=600)
    assert sum(s.items_dropped for s in full.sections) == 0
    assert tight.used_tokens <= tight.budget_tokens + 120  # only the incident header may overflow
    assert sum(s.items_dropped for s in tight.sections) > 0
    assert tight.section_names()[0] == "incident"
    assert len(tight.evidence_index) < len(full.evidence_index)
    assert estimate_tokens("abcd" * 10) == 10
    # Shares only redistribute budget, they never exceed it.
    shares = DiagnosisSettings(context_token_budget=800, section_shares=SectionShares(logs=5.0))
    _, inv, _, _ = _case(world)
    s, _, _, _ = _case(world)
    ctx = ContextBuilder(shares).build(incident=s.incident, events=s.events, investigation=inv)
    for st in ctx.sections:
        if st.name != "incident":
            assert st.used_tokens <= st.allocated_tokens


def test_context_without_graph_or_history_omits_those_sections(world):
    _, _, _, ctx = _case(world, include_graph=False, with_history=False)
    assert "graph" not in ctx.section_names() and "historical" not in ctx.section_names()
    assert "## GRAPH" not in ctx.text and "HISTORICAL" not in ctx.text
    # Resources named in evidence lines are still known resources.
    assert {ref.resource_id for ref in ctx.evidence_index.values()} <= set(ctx.resources)


# ------------------------------------------------------------------ prompts
def test_prompt_is_versioned_and_states_the_rules():
    assert PROMPT_VERSION == "diag-v1"
    assert re.fullmatch(r"[0-9a-f]{16}", prompt_sha()) and prompt_sha() == prompt_sha()
    for rule in ("never invent ids", "copy it exactly", "insufficient_evidence", "advisory only"):
        assert rule in SYSTEM_PROMPT
    req = build_request("## INCIDENT\nTitle: x", 0.0, 512, 7)
    assert req.json_mode and req.seed == 7 and "## INCIDENT" in req.prompt
    assert '"historical_influence"' in req.prompt
    rep = build_repair_request(req, "{bad", ["invalid JSON", "citation: evd_x unknown"])
    assert "invalid JSON" in rep.prompt and "{bad" in rep.prompt and req.prompt in rep.prompt
    assert rep.temperature == 0.0


# ------------------------------------------------------------------ validator and citations
def _valid_output(ctx, **changes):
    stub = StubLLM()
    req = build_request(ctx.text, 0.0, 2048, 0)
    data = json.loads(stub.complete(req).text)
    for k, v in changes.items():
        data[k] = v
    return data


def test_valid_stub_output_passes_and_cites_only_context_ids(world):
    _, _, retrieved, ctx = _case(world)
    out = validate_output(json.dumps(_valid_output(ctx)), ctx, retrieved, 0.6)
    assert out.valid, out.errors
    assert out.citation.cited and not out.citation.unknown and out.citation.unsupported_rate == 0
    assert out.citation.checked_values >= 0


@pytest.mark.parametrize(
    "text",
    ["The root cause is a deployment.", "[1, 2, 3]", '{"incident_summary": "x", ', "", "null"],
)
def test_validator_rejects_malformed_output(world, text):
    _, _, retrieved, ctx = _case(world)
    out = validate_output(text, ctx, retrieved, 0.6)
    assert not out.valid and out.diagnosis is None and out.errors


def test_validator_tolerates_code_fences(world):
    _, _, retrieved, ctx = _case(world)
    text = "```json\n" + json.dumps(_valid_output(ctx)) + "\n```"
    assert validate_output(text, ctx, retrieved, 0.6).valid
    assert parse_json('Here you go: {"a": 1} thanks')[0] == {"a": 1}


def test_validator_rejects_schema_violations(world):
    _, _, retrieved, ctx = _case(world)
    base = _valid_output(ctx)
    bad_label = dict(base, root_cause=dict(base["root_cause"], taxonomy_label="gremlins"))
    missing = {k: v for k, v in base.items() if k != "severity_suggestion"}
    over = dict(
        base,
        root_cause=dict(base["root_cause"], confidence=0.9),
        alternative_hypotheses=[
            {
                "taxonomy_label": "other",
                "description": "x",
                "confidence": 0.5,
                "rejected_because": "y",
                "label": "HYPOTHESIS",
            }
        ],
    )
    for data in (bad_label, missing, over):
        out = validate_output(json.dumps(data), ctx, retrieved, 0.6)
        assert not out.valid and any(e.startswith("schema:") for e in out.errors)


def test_validator_rejects_uncited_and_hallucinated_citations(world):
    _, _, retrieved, ctx = _case(world)
    base = _valid_output(ctx)
    uncited = dict(base, supporting_evidence=[])
    out = validate_output(json.dumps(uncited), ctx, retrieved, 0.6)
    assert not out.valid and any("supporting_evidence" in e for e in out.errors)
    fake = dict(
        base,
        supporting_evidence=[
            {"evidence_id": "evd_0000000000000000", "explanation": "made up", "label": "FACT"}
        ],
    )
    out = validate_output(json.dumps(fake), ctx, retrieved, 0.6)
    assert not out.valid and "evd_0000000000000000" in out.citation.unknown
    assert out.citation.unsupported_rate == 1.0
    hist = dict(
        base,
        supporting_evidence=[
            {"evidence_id": retrieved[0].incident_id, "explanation": "past case", "label": "FACT"}
        ],
    )
    out = validate_output(json.dumps(hist), ctx, retrieved, 0.6)
    assert not out.valid and any("historical" in e or "not an evidence id" in e for e in out.errors)


def test_validator_checks_quoted_values_in_facts_only(world):
    _, _, retrieved, ctx = _case(world)
    base = _valid_output(ctx)
    metric_ref = next(r for r in ctx.evidence_index.values() if r.value is not None)
    good = f"{metric_ref.metric} reached {metric_ref.value:g} at 12:03"
    wrong = f"{metric_ref.metric} reached {metric_ref.value * 3 + 11:g}"

    def with_item(expl, label):
        return dict(
            base,
            supporting_evidence=[
                {"evidence_id": metric_ref.evidence_id, "explanation": expl, "label": label}
            ],
        )

    assert validate_output(json.dumps(with_item(good, "FACT")), ctx, retrieved, 0.6).valid
    out = validate_output(json.dumps(with_item(wrong, "FACT")), ctx, retrieved, 0.6)
    assert not out.valid and out.citation.value_mismatches
    # INFERENCE may compute numbers (e.g. a rate), so it is not value-checked.
    assert validate_output(json.dumps(with_item(wrong, "INFERENCE")), ctx, retrieved, 0.6).valid


def test_validator_rejects_unknown_root_resource_and_unbacked_history(world):
    _, _, retrieved, ctx = _case(world)
    base = _valid_output(ctx)
    ghost = dict(base, root_cause=dict(base["root_cause"], resource_id="rds/not-in-context"))
    out = validate_output(json.dumps(ghost), ctx, retrieved, 0.6)
    assert not out.valid and any("does not appear in the context" in e for e in out.errors)
    _, _, _, no_hist = _case(world, with_history=False)
    claims = dict(
        _valid_output(no_hist),
        historical_influence={
            "used": True,
            "incident_ids": ["inc-x"],
            "how": "a past incident told me so",
        },
    )
    out = validate_output(json.dumps(claims), no_hist, None, 0.6)
    assert not out.valid and any("no historical incidents" in e for e in out.errors)


def test_insufficient_evidence_and_review_policy(world):
    _, _, retrieved, ctx = _case(world)
    base = _valid_output(ctx)
    ie = dict(
        base,
        root_cause={
            "taxonomy_label": "insufficient_evidence",
            "description": "not enough data",
            "confidence": 0.2,
            "resource_id": "anything",
            "label": "INFERENCE",
        },
        supporting_evidence=[],
        alternative_hypotheses=[],
        historical_influence={"used": False, "incident_ids": [], "how": ""},
        requires_human_review=True,
    )
    assert validate_output(json.dumps(ie), ctx, retrieved, 0.6).valid
    low = dict(
        base,
        root_cause=dict(base["root_cause"], confidence=0.4),
        alternative_hypotheses=[],
        requires_human_review=False,
    )
    out = validate_output(json.dumps(low), ctx, retrieved, 0.6)
    assert out.valid and out.diagnosis.requires_human_review and out.policy_overrides


# ------------------------------------------------------------------ engine, repair, sampling
def _engine(world, llm, samples=0):
    loader, collector, retriever, settings, pipeline, ids = world
    s = settings.model_copy(
        update={
            "diagnosis": settings.diagnosis.model_copy(update={"self_consistency_samples": samples})
        }
    )
    return DiagnosisEngine(llm, s, retriever, pipeline), collector, ids


def test_malformed_output_is_repaired_once(world):
    engine, collector, ids = _engine(world, StubLLM(script=["not json at all"]))
    r = engine.diagnose_offline(collector, ids[0])
    assert r.valid and [a.kind for a in r.attempts] == ["primary", "primary-repair"]
    assert not r.attempts[0].valid and r.attempts[1].valid
    repair_prompt = engine.llm.calls[1].prompt
    assert "rejected by the validator" in repair_prompt and "not json at all" in repair_prompt


def test_output_still_invalid_after_repair_is_rejected_with_a_recorded_failure(world):
    uncited = json.dumps(
        {
            "incident_summary": "x",
            "root_cause": {
                "taxonomy_label": "deployment_failure",
                "description": "d",
                "confidence": 0.8,
                "resource_id": "x",
                "label": "INFERENCE",
            },
            "supporting_evidence": [],
            "severity_suggestion": "HIGH",
            "requires_human_review": False,
        }
    )
    engine, collector, ids = _engine(world, StubLLM(script=[uncited, uncited]))
    r = engine.diagnose_offline(collector, ids[0])
    assert not r.valid and r.diagnosis is None and len(r.attempts) == 2
    assert r.failure["stage"] == "validation" and r.failure["raw_output"] == uncited
    assert any(e.startswith("after repair:") for e in r.failure["errors"])
    assert any(e.startswith("first attempt:") for e in r.failure["errors"])
    assert r.severity.level  # severity is still assessed deterministically


def test_self_consistency_and_advisory_severity(world):
    engine, collector, ids = _engine(world, StubLLM(), samples=4)
    r = engine.diagnose_offline(collector, ids[0])
    sc = r.self_consistency
    assert sc.samples == 4 and len(sc.labels) == 4 and sc.valid_samples == 4
    assert 0 <= sc.agreement_with_primary <= 1 and sc.majority_label
    assert r.verbalized_confidence == r.diagnosis.root_cause.confidence
    assert [a.kind for a in r.attempts] == [
        "primary",
        "sample-1",
        "sample-2",
        "sample-3",
        "sample-4",
    ]
    seeds = [c.seed for c in engine.llm.calls]
    assert seeds == [0, 1, 2, 3, 4] and engine.llm.calls[1].temperature > 0
    # The official severity is the engine's; the model's suggestion is only recorded.
    assert r.llm_severity_suggestion == r.diagnosis.severity_suggestion.value
    assert r.severity.method == "deterministic-v1"
    assert r.prompt_tokens > 0 and r.label == "smoke-test / synthetic"


def test_engine_is_deterministic(world):
    a = _engine(world, StubLLM(), samples=2)
    b = _engine(world, StubLLM(), samples=2)
    ra = a[0].diagnose_offline(a[1], a[2][3]).model_dump()
    rb = b[0].diagnose_offline(b[1], b[2][3]).model_dump()
    assert ra == rb


class RecordingExternal(LLMClient):
    is_external = True

    def __init__(self):
        self.stub = StubLLM()
        self.sent: list[LLMRequest] = []

    @property
    def model_name(self) -> str:
        return "recording-external"

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.sent.append(request)
        return self.stub.complete(request)


def test_external_llm_receives_redacted_prompts_and_answers_are_restored(world):
    loader, collector, retriever, settings, pipeline, ids = world
    iam = next(i for i in ids if any("123456789012" in e.message for e in loader.load(i).events))
    client = create_llm_client(RecordingExternal, settings)
    assert isinstance(client, RedactingLLMClient)
    engine = DiagnosisEngine(
        client,
        settings.model_copy(
            update={
                "diagnosis": settings.diagnosis.model_copy(update={"self_consistency_samples": 2})
            }
        ),
        retriever,
        pipeline,
    )
    r = engine.diagnose_offline(collector, iam)
    sent = client.inner.sent
    assert len(sent) == 3
    for req in sent:
        assert "123456789012" not in req.system + req.prompt
        assert "arn:aws:iam::123456789012" not in req.prompt
    # Same pseudonyms across the primary answer and the samples (one redactor per incident).
    tokens = [set(re.findall(r"\bACCOUNT_\d+\b", q.prompt)) for q in sent]
    assert tokens[0] and all(t == tokens[0] for t in tokens)
    assert r.valid and r.label == "llm"


# ------------------------------------------------------------------ LLM clients
def _recorder(response):
    calls = []

    def post(url, headers, body, timeout):
        calls.append((url, headers, body, timeout))
        if isinstance(response, Exception):
            raise response
        return response(len(calls)) if callable(response) else response

    return post, calls


OPENAI_OK = {
    "model": "gpt-x",
    "choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
}
GEMINI_OK = {
    "candidates": [
        {"content": {"parts": [{"text": '{"a"'}, {"text": ": 1}"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 100},
    "modelVersion": "gemini-x-001",
}


def test_openai_compatible_client_request_response_and_cost():
    post, calls = _recorder(OPENAI_OK)
    s = Settings(
        llm=LLMSettings(
            provider="openai_compatible",
            model="gpt-x",
            api_key="sk-test-123",
            input_cost_per_1k=0.5,
            output_cost_per_1k=1.5,
        )
    )
    client = build_llm_client(s, post=post)
    assert isinstance(client, RedactingLLMClient) and isinstance(
        client.inner, OpenAICompatibleClient
    )
    resp = client.complete(LLMRequest(system="sys", prompt="hi", seed=3, max_tokens=99))
    url, headers, body, _ = calls[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test-123"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["seed"] == 3 and body["max_tokens"] == 99
    assert body["response_format"] == {"type": "json_object"}
    assert resp.text == '{"a": 1}' and resp.prompt_tokens == 1200 and resp.completion_tokens == 300
    assert resp.estimated_cost_usd == pytest.approx(1.2 * 0.5 + 0.3 * 1.5)


def test_gemini_client_request_and_response():
    post, calls = _recorder(GEMINI_OK)
    s = Settings(llm=LLMSettings(provider="gemini", model="gemini-x", api_key="g-key-123456"))
    client = build_llm_client(s, post=post)
    resp = client.complete(LLMRequest(system="sys", prompt="hi", temperature=0.2))
    url, headers, body, _ = calls[0]
    assert url.endswith("/models/gemini-x:generateContent")
    assert headers == {"x-goog-api-key": "g-key-123456"}
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["temperature"] == 0.2
    assert resp.text == '{"a": 1}' and resp.prompt_tokens == 900 and resp.model == "gemini-x-001"


def test_clients_retry_transient_errors_and_surface_bad_responses():
    sleeps = []

    def flaky(n):
        if n < 3:
            raise urllib.error.HTTPError("u", 429, "slow down", {}, None)
        return OPENAI_OK

    post, calls = _recorder(flaky)
    s = Settings(llm=LLMSettings(provider="openai_compatible", api_key="sk-1234567", max_retries=3))
    client = create_llm_client(OpenAICompatibleClient, s, s.llm, post, sleeps.append)
    assert client.complete(LLMRequest(prompt="x")).text == '{"a": 1}'
    assert len(calls) == 3 and sleeps == [1.0, 2.0]
    post, _ = _recorder(urllib.error.HTTPError("u", 400, "bad request", {}, None))
    bad = create_llm_client(OpenAICompatibleClient, s, s.llm, post, sleeps.append)
    with pytest.raises(urllib.error.HTTPError):
        bad.complete(LLMRequest(prompt="x"))
    post, _ = _recorder({"unexpected": True})
    odd = create_llm_client(
        GeminiClient, s, LLMSettings(provider="gemini", api_key="k-123456"), post
    )
    with pytest.raises(LLMCallError):
        odd.complete(LLMRequest(prompt="x"))


def test_llm_clients_are_guarded_and_fail_closed_in_aws_mode():
    s = LLMSettings(provider="openai_compatible", api_key="sk-1234567")
    with pytest.raises(DirectConstructionError):
        OpenAICompatibleClient(s)
    with pytest.raises(DirectConstructionError):
        GeminiClient(s)
    assert isinstance(StubLLM(), StubLLM)  # LOCAL-ONLY: constructible directly
    aws = Settings(data_mode="aws", llm=s, redaction=RedactionSettings(strict=False))
    with pytest.raises(RedactionPolicyError):
        build_llm_client(aws, post=lambda *a: pytest.fail("must not be called"))
    assert isinstance(build_llm_client(Settings()), StubLLM)
    with pytest.raises(ValueError):
        build_llm_client(Settings(llm=LLMSettings(provider="crystal-ball")))


# ------------------------------------------------------------------ gate: end-to-end on dev
def test_gate_end_to_end_on_every_dev_incident_with_the_stub(dataset, tmp_path):
    settings = Settings(embeddings=EmbeddingSettings(provider="hashing"))
    payload = diagnosis_e2e.run(dataset, tmp_path / "hist", tmp_path / "out", settings, samples=1)
    rows = {r["condition"]: r for r in payload["conditions"]}
    loader = DatasetLoader(dataset)
    n_dev = len(loader.incident_ids("dev"))
    assert set(rows) == set(diagnosis_e2e.CONDITIONS)
    for r in rows.values():
        assert r["incidents"] == n_dev
        assert r["valid"] == n_dev and r["rejected"] == 0
        assert r["unsupported_citation_rate"] == 0.0
    assert rows["A1 no RAG"]["historical_used"] == 0
    assert sum(payload["severity"]["levels"].values()) == n_dev
    assert payload["meta"]["label"] == "smoke-test / synthetic"
    md = (tmp_path / "out" / "phase6-e2e-dev.md").read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in md and "not a result" in md


def test_committed_e2e_report_is_labelled():
    data = json.loads((diagnosis_e2e.DEFAULT_OUT / "phase6-e2e-dev.json").read_text("utf-8"))
    assert data["meta"]["label"] == "smoke-test / synthetic"
    assert all(r["rejected"] == 0 for r in data["conditions"])
