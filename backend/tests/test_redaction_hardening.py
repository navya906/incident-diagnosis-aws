"""Phase 5 hardening: values/timestamps untouched, secret formats, custom patterns, fail-closed
real-AWS mode, and the construction guard for external clients."""

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest
from pydantic import ValidationError

import app
from app.ai.clients import (
    RedactionPolicyError,
    create_embedder,
    create_llm_client,
    redaction_policy_violations,
)
from app.ai.redaction import RedactingLLMClient, RedactionError, Redactor
from app.config import (
    REQUIRED_AWS_CATEGORIES,
    CustomRedactionPattern,
    EmbeddingSettings,
    RedactionSettings,
    Settings,
)
from app.interfaces._guard import DirectConstructionError
from app.interfaces.embedder import Embedder
from app.interfaces.llm_client import LLMClient, LLMRequest, LLMResponse
from app.offline.dataset import DatasetLoader, generate_dataset
from app.rag.embedders import (
    GeminiEmbedder,
    HashingEmbedder,
    OpenAIEmbedder,
    RedactingEmbedder,
    build_embedder,
)

APP_DIR = Path(app.__file__).parent


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("hardening-ds")
    generate_dataset(out, seed=42)
    return out


# ------------------------------------------------------------------ (1) values and timestamps
NOT_IDENTIFIERS = [
    "2025-03-01T12:00:00Z",
    "2025-03-01T12:00:00.123456+00:00",
    "2025-03-01 12:00:00",
    "12:00:00",
    "12:00",
    "1741000000",  # epoch seconds (10 digits)
    "1741000000123",  # epoch milliseconds (13 digits)
    "0.000123",
    "1.5e-05",
    "99.99",
    "-3.25",
    "1200.0",
    "123456789012.5",  # 12 digits, but a decimal value, not an account id
    "4286 ms",
    "p99=812.5",
    "HTTPCode_Target_5XX_Count",
    "ev_0123456789abcdef",
    "ecs/service/web-1c1d",
    "da39a3ee5e6b4b0d3255bfef95601890afd80709",  # sha1 hex: not a secret key
    "sha256:3f1c9b2e",
]


@pytest.mark.parametrize("text", NOT_IDENTIFIERS, ids=range(len(NOT_IDENTIFIERS)))
def test_values_timestamps_and_names_are_not_redacted(text):
    r = Redactor()
    sentence = f"value {text} observed"
    assert r.redact(sentence) == sentence
    assert r.check(sentence) == []


def test_numbers_in_structured_data_are_never_touched():
    r = Redactor()
    obj = {"value": 123456789012, "ts": "2025-03-01T12:00:00+00:00", "nested": [0.5, 7, None]}
    assert r.redact_obj(obj) == obj


def test_dataset_metric_values_and_timestamps_survive_redaction(dataset):
    """Every event of every incident (dev and test): redacting the serialised incident leaves
    each event's timestamp, value, metric, event id and resource id byte-identical."""
    loader = DatasetLoader(dataset)
    checked = 0
    for iid in loader.incident_ids():
        raw = json.loads(loader.load(iid).model_dump_json())
        red = json.loads(Redactor().redact(json.dumps(raw)))
        assert red["incident"]["alarm_time"] == raw["incident"]["alarm_time"]
        for a, b in zip(raw["events"], red["events"], strict=True):
            for field in ("timestamp", "value", "metric", "event_id", "resource_id", "event_type"):
                assert a[field] == b[field], (iid, field, a[field], b[field])
            if a["source"] == "cloudwatch_metric":
                assert a["metadata"] == b["metadata"]
                checked += 1
    assert checked > 50_000


def test_structured_path_keeps_every_event_field_but_identities(dataset):
    loader = DatasetLoader(dataset)
    iid = loader.incident_ids("dev")[0]
    r = Redactor()
    for e in loader.load(iid).events:
        raw = e.model_dump(mode="json")
        red = r.redact_obj(raw)
        for field in ("timestamp", "value", "metric", "event_id", "resource_id", "severity"):
            assert red[field] == raw[field]
        if "userIdentity" in raw["metadata"]:
            assert raw["metadata"]["userIdentity"] not in json.dumps(red)


# ------------------------------------------------------------------ (2) secret formats
JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEiLCJpYXQiOjE3MDB9"
    ".SflKxwRJSMeKKF2QT4fwpM"
)
BEARER = "ya29.a0AfH6SMBx-very_long_opaque-token_value"
PEM_RSA = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7V1x\nq9Zt0aBcDeF+/gH==\n"
    "-----END RSA PRIVATE KEY-----"
)
PEM_OPENSSH = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
PEM_ENCRYPTED = (
    "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIFHDBOBgkqhkiG9w0BBQ0w\n"
    "-----END ENCRYPTED PRIVATE KEY-----"
)
PEM_GENERIC = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIFq3\n-----END PRIVATE KEY-----"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"token {JWT} expired", JWT),
        (f"GET /cb?id_token={JWT}&state=1", JWT),
        (f"Authorization: Bearer {BEARER}", BEARER),
        (f'{{"Authorization": "Bearer {BEARER}"}}', BEARER),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("dsn postgresql+psycopg://app:Pg-p4ss!@db.internal:5432/orders", "Pg-p4ss!"),
        ("mysql://root:r00tpass@10.0.0.5/app", "r00tpass"),
        ("mongodb+srv://svc:M0ng0Secret@cluster0.example.net/db", "M0ng0Secret"),
        ("redis://:R3disPass@cache:6379/0", "R3disPass"),
        ("amqp://guest:Gu3st-Pw@mq:5672/", "Gu3st-Pw"),
        ("jdbc:postgresql://h/db?user=app&password=JdbcPw99&ssl=true", "JdbcPw99"),
        ("Server=sql1;Database=x;User Id=sa;Password=AdoPw#1;", "AdoPw#1"),
        (f"key:\n{PEM_RSA}\nend", PEM_RSA),
        (PEM_OPENSSH, PEM_OPENSSH),
        (PEM_ENCRYPTED, PEM_ENCRYPTED),
        (PEM_GENERIC, PEM_GENERIC),
        (f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET),
        (f"leaked {AWS_SECRET} in a log line", AWS_SECRET),
        (f'"{AWS_SECRET}"', AWS_SECRET),
    ],
    ids=[
        "jwt",
        "jwt-in-url",
        "bearer",
        "bearer-json",
        "basic",
        "dsn-postgres",
        "mysql",
        "mongodb-srv",
        "redis-no-user",
        "amqp",
        "jdbc",
        "ado-net",
        "pem-rsa",
        "pem-openssh",
        "pem-encrypted",
        "pem-generic",
        "aws-secret-kv",
        "aws-secret-bare",
        "aws-secret-quoted",
    ],
)
def test_secret_formats_are_redacted_and_never_restored(text, secret):
    r = Redactor()
    out = r.redact(text)
    assert secret not in out
    assert "SECRET_" in out
    assert secret not in r.restore(out)  # secrets never come back
    assert r.check(out) == []


def test_secret_format_edges():
    r = Redactor()
    # Non-secret context around connection strings and JDBC parameters survives.
    out = r.redact("jdbc:postgresql://h/db?user=app&password=JdbcPw99&ssl=true")
    assert out.endswith("&ssl=true") and "user=app" in out
    out = r.redact("postgresql://app:pw12345@db:5432/orders")
    assert out == "postgresql://app:SECRET_2@db:5432/orders"
    # Certificates are public and stay; 41-character or longer base64 blobs are not bare keys.
    cert = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----"
    assert Redactor().redact(cert) == cert
    blob = AWS_SECRET + "Z"
    assert Redactor().redact(f"blob {blob}") == f"blob {blob}"
    lower = AWS_SECRET.lower()  # no upper-case letter: not treated as a key
    assert Redactor().redact(f"blob {lower}") == f"blob {lower}"
    # The bare-key category can be switched off on its own.
    off = Redactor(RedactionSettings(bare_secret_keys=False))
    assert AWS_SECRET in off.redact(f"leaked {AWS_SECRET}")


# ------------------------------------------------------------------ (3) custom patterns
def test_custom_patterns_validate_at_config_load():
    with pytest.raises(ValidationError):
        CustomRedactionPattern(name="TICKET", pattern="([unclosed")
    with pytest.raises(ValidationError):
        CustomRedactionPattern(name="ticket", pattern="TCK-\\d+")  # name must be upper case
    with pytest.raises(ValidationError):
        CustomRedactionPattern(name="EMPTY", pattern="x*")  # matches the empty string
    s = Settings(redaction={"custom_patterns": [{"name": "TICKET", "pattern": "TCK-\\d{4,}"}]})
    assert s.redaction.custom_patterns[0].name == "TICKET"


def test_custom_patterns_redact_consistently_restore_and_are_checked():
    st = RedactionSettings(
        custom_patterns=[
            CustomRedactionPattern(name="TICKET", pattern=r"\bTCK-\d{4,}\b"),
            CustomRedactionPattern(name="CUSTOMER", pattern=r"\bcust_[a-z0-9]{8}\b"),
            CustomRedactionPattern(
                name="LICENSE", pattern=r"license_key=(?P<value>[A-Z0-9-]{10,})", secret=True
            ),
        ]
    )
    r = Redactor(st)
    text = "TCK-12345 for cust_ab12cd34; again TCK-12345; license_key=ABCD-1234-EFGH"
    out = r.redact(text)
    assert out == "TICKET_1 for CUSTOMER_1; again TICKET_1; license_key=LICENSE_1"
    restored = r.restore(out)
    assert "TCK-12345" in restored and "cust_ab12cd34" in restored
    assert "ABCD-1234-EFGH" not in restored  # secret custom pattern is never restored
    with pytest.raises(RedactionError):
        r.ensure_safe("raw TCK-12345 slipped through")
    with pytest.raises(RedactionError):
        r.ensure_safe("an unseen TCK-99999")
    assert r.ensure_safe(out) == out
    # Custom tokens already in the text are not redacted again.
    assert r.redact(out) == out


# ------------------------------------------------------------------ fail closed in real-AWS mode
class CountingExternalLLM(LLMClient):
    is_external = True
    constructed = 0

    def __init__(self):
        type(self).constructed += 1
        self.sent: list[LLMRequest] = []

    @property
    def model_name(self) -> str:
        return "counting-external"

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.sent.append(request)
        return LLMResponse(text="ok", model=self.model_name)


def _aws(**redaction) -> Settings:
    return Settings(data_mode="aws", redaction=RedactionSettings(**redaction))


@pytest.mark.parametrize(
    "weaken",
    [{"enabled": False}, {"strict": False}, *({c: False} for c in REQUIRED_AWS_CATEGORIES)],
)
def test_real_aws_mode_fails_closed_before_constructing_anything(weaken):
    CountingExternalLLM.constructed = 0
    settings = _aws(**weaken)
    assert redaction_policy_violations(settings)
    with pytest.raises(RedactionPolicyError):
        create_llm_client(CountingExternalLLM, settings)
    assert CountingExternalLLM.constructed == 0
    emb = Settings(
        data_mode="aws",
        redaction=RedactionSettings(**weaken),
        embeddings=EmbeddingSettings(provider="openai", model="m", api_key="k-123456"),
    )
    with pytest.raises(RedactionPolicyError):
        build_embedder(emb, post=lambda *a: pytest.fail("must not send"))


def test_real_aws_mode_with_full_policy_wraps_and_strict_refusal_sends_nothing():
    client = create_llm_client(CountingExternalLLM, _aws())
    assert isinstance(client, RedactingLLMClient)
    assert redaction_policy_violations(_aws()) == []
    # resource_names may stay off: canonical ids carry no account data (D59).
    assert redaction_policy_violations(_aws(resource_names=False)) == []
    r = Redactor(client.settings)
    out = client.complete(LLMRequest(prompt="see 123456789012"), redactor=r)
    assert "123456789012" not in client.inner.sent[-1].prompt and out.text == "ok"
    # An identity learned from structured data (userIdentity) and then repeated in free text,
    # where no pattern recognises it: strict mode refuses and nothing is sent.
    r.redact_obj({"userIdentity": "svc-deployer-01"})
    before = len(client.inner.sent)
    with pytest.raises(RedactionError):
        client.complete(LLMRequest(prompt="change made by svc-deployer-01"), redactor=r)
    assert len(client.inner.sent) == before


def test_offline_mode_allows_disabling_redaction_but_aws_mode_does_not():
    off = Settings(redaction=RedactionSettings(enabled=False))
    assert isinstance(create_llm_client(CountingExternalLLM, off), CountingExternalLLM)
    with pytest.raises(RedactionPolicyError):
        create_llm_client(CountingExternalLLM, off.model_copy(update={"data_mode": "aws"}))


# ------------------------------------------------------------------ (4) construction guard
LOCAL_ALLOWLIST = {"HashingEmbedder", "SentenceTransformerEmbedder"}
WRAPPERS = {"RedactingLLMClient", "RedactingEmbedder"}


def _all_app_subclasses(base):
    for mod in pkgutil.walk_packages([str(APP_DIR)], prefix="app."):
        importlib.import_module(mod.name)
    found, stack = set(), [base]
    while stack:
        for sub in stack.pop().__subclasses__():
            stack.append(sub)
            if sub.__module__.startswith("app."):
                found.add(sub)
    return found


def test_external_clients_cannot_be_constructed_directly():
    with pytest.raises(DirectConstructionError):
        CountingExternalLLM()
    s = EmbeddingSettings(provider="openai", model="m", api_key="k-123456")
    with pytest.raises(DirectConstructionError):
        OpenAIEmbedder(s)
    with pytest.raises(DirectConstructionError):
        GeminiEmbedder(s)
    # Through the factory: always wrapped.
    built = create_embedder(OpenAIEmbedder, Settings(), s, lambda *a: {"data": []})
    assert isinstance(built, RedactingEmbedder)
    assert isinstance(create_embedder(HashingEmbedder, Settings(), 16), HashingEmbedder)


@pytest.mark.parametrize("base", [LLMClient, Embedder])
def test_every_app_client_is_either_allowlisted_local_a_wrapper_or_gated(base):
    """Covers implementations added later (e.g. Phase 6 LLM clients) automatically: a new
    in-process class must be added to LOCAL_ALLOWLIST on purpose; anything else is gated."""
    for cls in _all_app_subclasses(base):
        name = cls.__name__
        if name in WRAPPERS:
            assert cls.is_redaction_wrapper is True
            continue
        if not cls.is_external:
            assert name in LOCAL_ALLOWLIST, f"{name} claims to be in-process; review it"
            continue
        if inspect.isabstract(cls):
            continue
        with pytest.raises(DirectConstructionError):
            cls()


def test_factory_key_is_only_referenced_by_the_guard_and_the_factory():
    allowed = {APP_DIR / "interfaces" / "_guard.py", APP_DIR / "ai" / "clients.py"}
    users = {p for p in APP_DIR.rglob("*.py") if "_FACTORY_KEY" in p.read_text(encoding="utf-8")}
    assert users == allowed
    assert "_factory_key=" not in (APP_DIR / "rag" / "embedders.py").read_text(encoding="utf-8")
