"""Phase 5: redaction, embeddings, vector stores, historical knowledge base and retrieval."""

import copy
import json
import os
import re

import pytest
from sqlalchemy import create_engine

from app.ai.clients import create_llm_client
from app.ai.redaction import (
    RedactingLLMClient,
    RedactionError,
    Redactor,
)
from app.config import EmbeddingSettings, RagSettings, RedactionSettings, Settings
from app.contracts import Diagnosis
from app.contracts.historical import RetrievedIncident
from app.contracts.taxonomy import FAULT_TYPES
from app.db import Base
from app.evaluation import retrieval_eval
from app.evidence.pipeline import InvestigationPipeline
from app.interfaces.llm_client import LLMClient, LLMRequest, LLMResponse
from app.offline.dataset import DatasetLoader, generate_dataset
from app.offline.replay import ReplayCollector
from app.rag.corpus import (
    build_corpus,
    heldout_fingerprints,
    historical_plan,
    load_corpus,
    record_from_scenario,
    records_from_dataset,
)
from app.rag.embedders import (
    GeminiEmbedder,
    HashingEmbedder,
    OpenAIEmbedder,
    RedactingEmbedder,
    build_embedder,
    tokenize,
)
from app.rag.guidance import (
    HISTORICAL_GUIDANCE,
    render_historical_section,
    validate_historical_influence,
)
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase, LeakageError, build_query
from app.rag.vector_store import LocalVectorStore, PgVectorStore, build_vector_store

ACCOUNT = "123456789012"
OTHER_ACCOUNT = "210987654321"
USER_ARN = f"arn:aws:iam::{ACCOUNT}:user/deployer"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/web-task-exec"
SVC_ARN = f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/prod/web-1c1d"
IP4, IP6 = "10.0.3.17", "2001:db8::8a2e:370:7334"
HOST = "db-main.c9abcdefgh12.us-east-1.rds.amazonaws.com"
ACCESS_KEY, ROLE_ID = "AKIAIOSFODNN7EXAMPLE", "AROA1234567890ABCDEFG"
EMAIL = "oncall.engineer@example.com"
PASSWORD, API_KEY, BEARER = (
    "hunter2-Prod!",
    "sk-live-0123456789abcdef",
    "abcdefghijklmnopqrstuvwxyz",
)
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
URL_PW = "s3cr3tDbPass"

SAMPLE = (
    f"{USER_ARN} assumed {ROLE_ARN} and called UpdateService on {SVC_ARN} from {IP4} and {IP6}. "
    f"DB endpoint {HOST}; access key {ACCESS_KEY}; role id {ROLE_ID}; contact {EMAIL}; "
    f'other account {OTHER_ACCOUNT}. Config: password={PASSWORD} "api_key": "{API_KEY}" '
    f"Authorization: Bearer {BEARER} postgres://app:{URL_PW}@db:5432/orders token {JWT}. "
    "At 2025-03-01T12:00:00Z (12:00:00) ecs/service/web-1c1d CPUUtilization was 93.5; "
    "event ev_0123456789abcdef, version 1.2.3.4.5, latency 0.0123 s."
)
RAW_VALUES = [
    ACCOUNT,
    OTHER_ACCOUNT,
    "user/deployer",
    "role/web-task-exec",
    "service/prod/web-1c1d",
    IP4,
    IP6,
    HOST,
    ACCESS_KEY,
    ROLE_ID,
    EMAIL,
    PASSWORD,
    API_KEY,
    BEARER,
    JWT,
    URL_PW,
]


# ------------------------------------------------------------------ redaction
def test_every_category_is_redacted_and_nothing_raw_remains():
    r = Redactor()
    out = r.redact(SAMPLE)
    for raw in RAW_VALUES:
        assert raw not in out, raw
    for token in ("ACCOUNT_1", "ACCOUNT_2", "PRINCIPAL_", "RESOURCE_", "IP_", "HOST_1"):
        assert token in out
    assert "ACCESS_KEY_1" in out and "EMAIL_1" in out and "SECRET_" in out
    assert r.check(out) == []


def test_structure_and_non_identifiers_are_preserved():
    out = Redactor().redact(SAMPLE)
    # ARN partition/service/region survive, so the model still knows what kind of thing it is.
    assert "arn:aws:iam::ACCOUNT_1:user/PRINCIPAL_" in out
    assert "arn:aws:ecs:us-east-1:ACCOUNT_1:service/RESOURCE_" in out
    # Timestamps, times, event ids, canonical resource ids and numbers are not identifiers.
    for keep in (
        "2025-03-01T12:00:00Z",
        "(12:00:00)",
        "ev_0123456789abcdef",
        "ecs/service/web-1c1d",
        "93.5",
        "0.0123",
        "password=",
        "Bearer ",
    ):
        assert keep in out, keep


def test_pseudonyms_are_consistent_and_restorable_except_secrets():
    r = Redactor()
    a = r.redact(f"{ACCOUNT} then {IP4} then {ACCOUNT} and {OTHER_ACCOUNT}")
    b = r.redact(f"later {IP4} and {ACCOUNT}")
    assert a == "ACCOUNT_1 then IP_1 then ACCOUNT_1 and ACCOUNT_2"
    assert b == "later IP_1 and ACCOUNT_1"
    full = r.redact(SAMPLE)
    restored = r.restore(full)
    for raw in RAW_VALUES:
        secret = raw in (PASSWORD, API_KEY, BEARER, JWT, URL_PW)
        assert (raw in restored) is (not secret), raw
    # Two sessions use independent mappings.
    assert Redactor().redact(OTHER_ACCOUNT) == "ACCOUNT_1"


def test_categories_can_be_switched_off_and_disabled_is_passthrough():
    only = RedactionSettings(ips=False, account_ids=False)
    out = Redactor(only).redact(f"{IP4} {OTHER_ACCOUNT} {EMAIL}")
    assert IP4 in out and OTHER_ACCOUNT in out and EMAIL not in out
    assert Redactor(RedactionSettings(enabled=False)).redact(SAMPLE) == SAMPLE
    names = Redactor(RedactionSettings(resource_names=True)).redact("rds/db-1c1d and rds/db-1c1d")
    assert names == "rds/RESOURCE_1 and rds/RESOURCE_1"


def test_structured_identity_fields_are_pseudonymised_whole():
    r = Redactor()
    obj = {
        "userIdentity": "deployer",
        "sourceIPAddress": "internal.example",
        "requestParameters": {"note": f"from {IP4}", "count": 3},
        "list": [ACCOUNT, 1.5],
    }
    out = r.redact_obj(obj)
    assert out["userIdentity"].startswith("PRINCIPAL_")
    assert out["sourceIPAddress"].startswith("IP_")
    # sourceIPAddress came first and took IP_1; the address inside the note is a new value.
    assert out["requestParameters"] == {"note": "from IP_2", "count": 3}
    assert out["list"] == ["ACCOUNT_1", 1.5]
    assert r.restore_obj(out)["userIdentity"] == "deployer"


def test_strict_check_refuses_text_with_raw_values():
    r = Redactor()
    r.redact(f"account {ACCOUNT}")
    with pytest.raises(RedactionError):
        r.ensure_safe(f"oops the raw value {ACCOUNT} slipped through")
    with pytest.raises(RedactionError):
        r.ensure_safe(f"an unseen identifier {IP4}")
    assert r.ensure_safe("ACCOUNT_1 is fine") == "ACCOUNT_1 is fine"
    lax = Redactor(RedactionSettings(strict=False))
    assert lax.ensure_safe(IP4) == IP4


class RecordingLLM(LLMClient):
    """Stands in for an external provider: records exactly what would leave the process."""

    is_external = True

    def __init__(self):
        self.sent: list[LLMRequest] = []

    @property
    def model_name(self) -> str:
        return "recording-external"

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.sent.append(request)
        tokens = sorted(set(re.findall(r"\b(?:ACCOUNT|PRINCIPAL|IP)_\d+\b", request.prompt)))
        return LLMResponse(text="Suspects: " + ", ".join(tokens), model=self.model_name)


class LocalLLM(RecordingLLM):
    is_external = False


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("phase5-ds")
    generate_dataset(out, seed=42)
    return out


def test_gate_no_raw_identifiers_leave_the_process_via_llm(dataset):
    """Gate: with redaction on, an external LLM receives no raw identifier."""
    loader = DatasetLoader(dataset)
    client = create_llm_client(RecordingLLM, Settings())
    assert isinstance(client, RedactingLLMClient)
    inner = client.inner
    iid = loader.incident_ids("dev")[0]
    scenario = loader.load(iid).model_dump(mode="json")
    context = json.dumps(scenario["events"][-60:]) + "\n" + SAMPLE
    resp = client.complete(LLMRequest(system=f"You are on-call for {ACCOUNT}.", prompt=context))
    sent = inner.sent[0].system + inner.sent[0].prompt
    for raw in RAW_VALUES:
        assert raw not in sent, raw
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", sent)
    assert not re.search(r"arn:aws[^\s\"]*:\d{12}:", sent)
    # The answer comes back with real identifiers, so citations and resource ids still work.
    assert ACCOUNT in resp.text and "ACCOUNT_" not in resp.text


def test_gate_whole_dataset_redacts_cleanly(dataset):
    """Every observable incident (dev and test) serialised for a prompt passes the strict check."""
    loader = DatasetLoader(dataset)
    for iid in loader.incident_ids():
        r = Redactor()
        text = loader.load(iid).model_dump_json()
        out = r.redact(text)
        assert r.check(out) == [], iid
        assert ACCOUNT not in out and "user/deployer" not in out


def test_factory_wraps_only_external_clients():
    assert isinstance(create_llm_client(LocalLLM, Settings()), LocalLLM)
    # Offline mode may switch redaction off for synthetic data: the client is then unwrapped.
    off = create_llm_client(RecordingLLM, Settings(redaction=RedactionSettings(enabled=False)))
    assert isinstance(off, RecordingLLM)


def test_shared_redactor_keeps_pseudonyms_across_calls():
    client = create_llm_client(RecordingLLM, Settings())
    inner = client.inner
    r = Redactor()
    client.complete(LLMRequest(prompt=f"first {IP4}"), redactor=r)
    client.complete(LLMRequest(prompt=f"repair: {OTHER_ACCOUNT} and {IP4}"), redactor=r)
    assert inner.sent[0].prompt == "first IP_1"
    assert inner.sent[1].prompt == "repair: ACCOUNT_1 and IP_1"


# ------------------------------------------------------------------ embedders
def test_hashing_embedder_is_deterministic_normalised_and_meaningful():
    e = HashingEmbedder(64)
    assert e.model_name == "hashing-v1-64" and e.dimension == 64 and e.is_external is False
    a, b, c = e.embed(
        [
            "Task timed out after 3.00 seconds on lambda",
            "Task timed out after 30.00 seconds on lambda",
            "UpdateService registered a new task definition",
        ]
    )
    assert a == e.embed(["Task timed out after 3.00 seconds on lambda"])[0]
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9

    def dot(x, y):
        return sum(p * q for p, q in zip(x, y, strict=True))

    assert dot(a, b) > dot(a, c)
    assert tokenize("HTTPCode_Target_5XX_Count") == ["http", "code", "target", "5", "xx", "count"]
    with pytest.raises(ValueError):
        HashingEmbedder(4)


def _fake_post(capture):
    def post(url, headers, body, timeout):
        capture.append((url, headers, body))
        if "batchEmbedContents" in url:
            return {"embeddings": [{"values": [1.0, 0.0, 0.5]} for _ in body["requests"]]}
        return {
            "data": [{"index": i, "embedding": [0.0, 1.0, 0.5]} for i in range(len(body["input"]))]
        }

    return post


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_gate_external_embedders_receive_only_redacted_text(provider):
    captured: list = []
    settings = Settings(
        embeddings=EmbeddingSettings(provider=provider, model="m", api_key="key-123456")
    )
    emb = build_embedder(settings, post=_fake_post(captured))
    assert isinstance(emb, RedactingEmbedder)
    assert isinstance(emb.inner, OpenAIEmbedder if provider == "openai" else GeminiEmbedder)
    vecs = emb.embed([SAMPLE, f"plain text from {IP4}"])
    assert len(vecs) == 2 and emb.dimension == 3 and emb.model_name == "m"
    body = json.dumps([c[2] for c in captured])
    for raw in RAW_VALUES:
        assert raw not in body, raw
    assert "key-123456" in json.dumps([c[1] for c in captured])  # the API key goes in a header


def test_build_embedder_local_and_errors():
    s = Settings(embeddings=EmbeddingSettings(provider="hashing", dimension=32))
    assert isinstance(build_embedder(s), HashingEmbedder)
    with pytest.raises(ValueError):
        build_embedder(Settings(embeddings=EmbeddingSettings(provider="word2vec")))
    with pytest.raises(ValueError):
        build_embedder(Settings(embeddings=EmbeddingSettings(provider="openai")))  # no key


# ------------------------------------------------------------------ vector stores
VECS = {"a": [1.0, 0.0, 0.0], "b": [0.9, 0.1, 0.0], "c": [0.0, 1.0, 0.0], "d": [0.0, 0.0, 1.0]}


def _fill(store):
    store.create_index("ix", "model-x", 3)
    ids = list(VECS)
    store.add("ix", ids, [VECS[i] for i in ids], [{"n": i} for i in ids], "model-x")
    return store


def _stores(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'v.db'}")
    Base.metadata.create_all(engine)
    stores = [LocalVectorStore(use_faiss=False), PgVectorStore(engine)]
    try:
        stores.append(LocalVectorStore(use_faiss=True))
    except RuntimeError:
        pass  # faiss-cpu not installed: the numpy path is still covered
    return stores


def test_vector_stores_agree_and_enforce_model_metadata(tmp_path):
    results = []
    for store in _stores(tmp_path):
        _fill(store)
        hits = store.search("ix", [1.0, 0.05, 0.0], 3, "model-x")
        results.append([(h.id, round(h.score, 4)) for h in hits])
        assert hits[0].payload == {"n": "a"}
        assert [h.id for h in store.search("ix", [1, 0, 0], 2, "model-x", {"a"})] == ["b", "c"]
        assert store.ids("ix") == set(VECS)
        assert store.index_metadata("ix").model_name == "model-x"
        with pytest.raises(ValueError):
            store.search("ix", [1, 0, 0], 2, "another-model")
        with pytest.raises(ValueError):
            store.add("ix", ["e"], [[1.0, 0.0]], [{}], "model-x")  # wrong dimension
        with pytest.raises(ValueError):
            store.add("ix", ["a"], [[1.0, 0.0, 0.0]], [{}], "model-x")  # duplicate id
        with pytest.raises(ValueError):
            store.add("ix", ["e"], [[1.0, 0.0, 0.0]], [{}], "another-model")
        with pytest.raises(ValueError):
            store.create_index("ix", "another-model", 3)
    assert all(r == results[0] for r in results)
    assert results[0][0][0] == "a" and results[0][1][0] == "b"


def test_local_store_persists_and_factory(tmp_path):
    store = _fill(LocalVectorStore(tmp_path / "vs", use_faiss=False))
    store.save()
    again = LocalVectorStore(tmp_path / "vs", use_faiss=False)
    assert again.index_metadata("ix") == store.index_metadata("ix")
    assert again.search("ix", [0, 1, 0], 1, "model-x")[0].id == "c"
    assert build_vector_store("numpy").backend == "numpy"
    with pytest.raises(ValueError):
        build_vector_store("pgvector")
    with pytest.raises(ValueError):
        build_vector_store("milvus")


@pytest.mark.skipif(
    not os.environ.get("CLOUDDIAG_TEST_PG_URL"),
    reason="set CLOUDDIAG_TEST_PG_URL to a migrated PostgreSQL+pgvector database",
)
def test_pgvector_store_on_postgres():  # pragma: no cover - needs a live database
    engine = create_engine(os.environ["CLOUDDIAG_TEST_PG_URL"])
    store = PgVectorStore(engine)
    name = f"ix-test-{os.getpid()}"
    store.create_index(name, "model-x", 3)
    ids = list(VECS)
    store.add(name, ids, [VECS[i] for i in ids], [{"n": i} for i in ids], "model-x")
    assert [h.id for h in store.search(name, [1.0, 0.05, 0.0], 2, "model-x")] == ["a", "b"]
    assert [h.id for h in store.search(name, [1, 0, 0], 1, "model-x", {"a"})] == ["b"]


# ------------------------------------------------------------------ corpus and leakage
def test_corpus_is_separate_deterministic_and_covers_every_label(tmp_path):
    a = build_corpus(tmp_path / "a")
    b = build_corpus(tmp_path / "b")
    assert (tmp_path / "a" / "corpus.json").read_bytes() == (
        tmp_path / "b" / "corpus.json"
    ).read_bytes()
    assert a == b == load_corpus(tmp_path / "a")
    assert len(a) == len(historical_plan()) == 40
    assert {r.taxonomy_label for r in a} == set(FAULT_TYPES)
    assert all(s.key.startswith("hist:") for s in historical_plan())
    assert all(r.label == "smoke-test / synthetic" and r.source == "historical_corpus" for r in a)
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert manifest["seed"] == 7 and manifest["count"] == 40
    # search_text is the observable part; the root cause only appears in the summary.
    r = a[0]
    assert r.root_cause_text not in r.search_text and r.root_cause_text in r.summary


def test_gate_test_incidents_never_enter_the_index(dataset, tmp_path):
    """Gate: leakage. The index holds no test incident, id or event."""
    loader = DatasetLoader(dataset)
    forbidden = heldout_fingerprints(loader)
    test_ids = set(loader.incident_ids("test"))
    assert test_ids and test_ids <= forbidden
    corpus = build_corpus(tmp_path / "hist")
    for rec in corpus:
        assert not forbidden & set(rec.fingerprints)
    kb = KnowledgeBase(HashingEmbedder(128), LocalVectorStore(use_faiss=False), "h", forbidden)
    assert kb.build(corpus) == 40
    assert not kb.indexed_ids() & test_ids
    # Dev leave-one-out knowledge base: dev only, never test.
    dev_records = records_from_dataset(loader, "dev")
    assert {r.incident_id for r in dev_records} <= set(loader.incident_ids("dev"))
    loo = KnowledgeBase(HashingEmbedder(128), LocalVectorStore(use_faiss=False), "d", forbidden)
    loo.build(dev_records)
    assert not loo.indexed_ids() & test_ids
    with pytest.raises(ValueError):
        records_from_dataset(loader, "test")
    # A record made from a test incident is refused before anything is written.
    tid = sorted(test_ids)[0]
    leaked = record_from_scenario(loader.load(tid), loader.load_truth(tid), "x", "dataset_dev")
    guarded = KnowledgeBase(HashingEmbedder(128), LocalVectorStore(use_faiss=False), "g", forbidden)
    with pytest.raises(LeakageError):
        guarded.build([corpus[0], leaked])
    with pytest.raises(KeyError):
        guarded.indexed_ids()  # nothing was indexed
    # Even with a different id, the copied events/description give it away.
    disguised = leaked.model_copy(update={"incident_id": "inc-disguised"})
    with pytest.raises(LeakageError):
        guarded.build([disguised])


def test_one_index_never_mixes_embedding_models(tmp_path):
    corpus = build_corpus(tmp_path / "hist")
    store = LocalVectorStore(use_faiss=False)
    KnowledgeBase(HashingEmbedder(128), store, "h").build(corpus[:5])
    with pytest.raises(ValueError):
        KnowledgeBase(HashingEmbedder(64), store, "h").build(corpus[5:10])


# ------------------------------------------------------------------ retrieval
@pytest.fixture(scope="module")
def retrieval_setup(dataset, tmp_path_factory):
    loader = DatasetLoader(dataset)
    corpus = build_corpus(tmp_path_factory.mktemp("hist"))
    kb = KnowledgeBase(
        HashingEmbedder(384), LocalVectorStore(), "historical", heldout_fingerprints(loader)
    )
    kb.build(corpus)
    return loader, kb


def _dev_query(loader, iid):
    inv = InvestigationPipeline(Settings()).run_offline(ReplayCollector(loader), iid)
    s = loader.load(iid)
    return build_query(s.incident, s.events, [i.event_id for i in inv.ranking.items], s.resources)


def test_retrieval_finds_same_fault_type_and_reranks(retrieval_setup):
    loader, kb = retrieval_setup
    retriever = HistoricalRetriever(kb, RagSettings())
    ids = [
        i
        for i in loader.incident_ids("dev")
        if loader.load_truth(i).ground_truth.evidence_event_ids
    ][:12]
    hits_at_1 = 0
    for iid in ids:
        q = _dev_query(loader, iid)
        assert q.signals and q.alarm_metric and "Key signals:" in q.text
        out = retriever.retrieve(q)
        assert [r.rank for r in out] == [1, 2, 3]
        assert [r.score for r in out] == sorted((r.score for r in out), reverse=True)
        for r in out:
            assert r.label == "HISTORICAL"
            assert 0 <= r.signal_overlap <= 1 and 0 <= r.context_match <= 1
            w = RagSettings().rerank
            expected = w.similarity * r.similarity + w.signals * r.signal_overlap
            assert r.score == pytest.approx(expected + w.context * r.context_match, abs=1e-5)
        assert out == retriever.retrieve(q)  # deterministic
        hits_at_1 += out[0].taxonomy_label == loader.load_truth(iid).ground_truth.taxonomy_label
    assert hits_at_1 / len(ids) >= 0.75


def test_leave_one_out_never_returns_the_query(dataset):
    loader = DatasetLoader(dataset)
    kb = KnowledgeBase(HashingEmbedder(256), LocalVectorStore(use_faiss=False), "loo")
    kb.build(records_from_dataset(loader, "dev"))
    retriever = HistoricalRetriever(kb, RagSettings(top_k=5))
    for iid in sorted(kb.records)[:10]:
        rec = kb.records[iid]
        out = retriever.retrieve(build_query_from_record(rec, iid), k=5)
        assert iid not in {r.incident_id for r in out} and len(out) == 5
    extra = retriever.retrieve(build_query_from_record(rec, None), k=3, exclude_ids={iid})
    assert iid not in {r.incident_id for r in extra}


def build_query_from_record(rec, incident_id):
    from app.rag.knowledge_base import RetrievalQuery

    return RetrievalQuery(
        text=rec.search_text,
        signals=set(rec.signals),
        alarm_metric=rec.alarm_metric,
        resource_types=set(rec.resource_types),
        incident_id=incident_id,
    )


def test_retrieval_eval_writes_labelled_report_with_zero_leakage(dataset, tmp_path):
    settings = Settings(embeddings=EmbeddingSettings(provider="hashing", dimension=128))
    payload = retrieval_eval.run(dataset, tmp_path / "hist", tmp_path / "out", settings, limit=10)
    leak = payload["leakage"]
    assert leak["test_ids_in_corpus_index"] == 0 and leak["test_ids_in_loo_index"] == 0
    assert leak["fingerprint_overlap_corpus"] == 0
    assert all(r["query_returned_itself"] == 0 for r in payload["results"])
    assert len(payload["results"]) == 8
    md = (tmp_path / "out" / "phase5-retrieval-dev.md").read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in md and "hashing-v1-128" in md


def test_committed_retrieval_report_is_labelled_and_leak_free():
    path = retrieval_eval.DEFAULT_OUT / "phase5-retrieval-dev.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["meta"]["label"] == "smoke-test / synthetic"
    assert payload["leakage"]["test_ids_in_corpus_index"] == 0
    assert payload["leakage"]["fingerprint_overlap_corpus"] == 0


# ------------------------------------------------------------------ do not blindly reuse
DIAG = {
    "incident_summary": "5xx errors on the ALB after a deployment.",
    "root_cause": {
        "taxonomy_label": "deployment_failure",
        "description": "Task definition web:42 crashes on start after the 12:00 rollout.",
        "confidence": 0.7,
        "resource_id": "ecs/service/web-1c1d",
        "label": "INFERENCE",
    },
    "supporting_evidence": [
        {"evidence_id": "evd_1", "explanation": "UpdateService at 12:00", "label": "FACT"}
    ],
    "contradicting_evidence": [],
    "contributing_factors": [],
    "alternative_hypotheses": [],
    "historical_influence": {"used": False, "incident_ids": [], "how": ""},
    "impact_analysis": {"services": [], "resources": [], "blast_radius": "", "user_impact": ""},
    "severity_suggestion": "HIGH",
    "recommendations": [],
    "missing_information": [],
    "requires_human_review": False,
}
PAST = RetrievedIncident(
    incident_id="inc-past1",
    rank=1,
    score=0.8,
    similarity=0.8,
    signal_overlap=0.5,
    context_match=1.0,
    taxonomy_label="deployment_failure",
    title="ALARM: 5XX on alb/x",
    summary="Past: a bad deployment caused 5XX. Root cause: new revision crashed.",
    root_cause_text="A new task definition revision was deployed and its containers crash.",
    resolution="Roll back the service.",
)


def diag(**changes):
    d = copy.deepcopy(DIAG)
    for k, v in changes.items():
        d[k] = v
    return Diagnosis.model_validate(d)


def test_historical_section_states_rules_and_labels_past_incidents():
    text = render_historical_section([PAST])
    assert text.startswith(HISTORICAL_GUIDANCE)
    assert "NOT evidence" in text and "[inc-past1] (HISTORICAL" in text
    assert "Never cite a historical" in text and "historical_influence" in text
    assert "no similar past incidents" in render_historical_section([])


def test_historical_influence_validation():
    assert validate_historical_influence(diag(), [PAST]) == []
    used = {
        "used": True,
        "incident_ids": ["inc-past1"],
        "how": "Suggested checking the rollout; UpdateService evd_1 confirms it.",
    }
    assert validate_historical_influence(diag(historical_influence=used), [PAST]) == []
    cited = diag(
        supporting_evidence=[{"evidence_id": "inc-past1", "explanation": "x", "label": "FACT"}]
    )
    assert any("cited as evidence" in p for p in validate_historical_influence(cited, [PAST]))
    unknown = {"used": True, "incident_ids": ["inc-other"], "how": "x" * 30}
    problems = validate_historical_influence(diag(historical_influence=unknown), [PAST])
    assert any("not retrieved" in p for p in problems)
    lazy = {"used": True, "incident_ids": ["inc-past1"], "how": "similar"}
    assert any(
        "'how'" in p for p in validate_historical_influence(diag(historical_influence=lazy), [PAST])
    )
    copied_rc = dict(DIAG["root_cause"], description=PAST.root_cause_text)
    problems = validate_historical_influence(diag(root_cause=copied_rc), [PAST])
    assert any("copies past incident" in p for p in problems)
    assert any("not declared" in p for p in problems)
