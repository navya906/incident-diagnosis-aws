"""Redaction with consistent pseudonyms, applied before ANY external LLM or embedding call.

A `Redactor` holds one session's mapping (raw value <-> pseudonym), so the same account id, ARN,
IP or principal always gets the same token (``ACCOUNT_1``, ``IP_2``, ``PRINCIPAL_1`` ...) and
the LLM can still reason about "the same role" across the prompt. `restore` maps tokens in the
model's answer back to the real values; secrets are never restored.

Categories (each can be switched off in config):

  secrets         key=value secrets, bearer tokens, JWTs, PEM private keys, URL passwords
  arns            whole ARNs; partition, service and region are kept, the account becomes its
                  ACCOUNT token and the resource part a RESOURCE / PRINCIPAL token
  principals      IAM unique ids (AIDA/AROA/...), access key ids (AKIA/ASIA), e-mail addresses,
                  and the values of identity keys in structured data (userIdentity, ...)
  hostnames       *.amazonaws.com and *.compute.internal names
  ips             IPv4 and IPv6 addresses
  account_ids     standalone 12-digit numbers
  resource_names  canonical resource ids (rds/db-1c1d -> rds/RESOURCE_1); off by default

Guarantee (strict mode): `check` re-scans outgoing text for every raw value this redactor has
replaced and for any detectable identifier still present; `RedactingLLMClient` and
`RedactingEmbedder` refuse to send text that fails it (`RedactionError`). See DECISIONS D59.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import RedactionSettings
from app.interfaces.llm_client import LLMClient, LLMRequest, LLMResponse

TOKEN = re.compile(r"\b(?:ACCOUNT|ARN|RESOURCE|PRINCIPAL|ACCESS_KEY|EMAIL|HOST|IP|SECRET)_\d+\b")

_SECRET_KEYS = (
    r"aws_secret_access_key|secret_access_key|secretaccesskey|client_secret|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token|refresh[_-]?token|session[_-]?token|password|passwd|"
    r"pwd|secret|token"
)
_SECRET_KV = re.compile(
    rf"(?i)(?P<key>\b(?:{_SECRET_KEYS})\b[\"']?)(?P<sep>\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s\"',;}\]]{3,})"
)
_BEARER = re.compile(r"(?i)\b(?P<key>bearer|basic)(?P<sep>\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_URL_PASSWORD = re.compile(
    r"(?P<key>\b[a-z][a-z0-9+.-]*://[^:/\s@]+)(?P<sep>:)(?P<value>[^@\s/]+)(?=@)"
)
_ARN = re.compile(
    r"\barn:(?P<partition>aws[a-z-]*):(?P<service>[a-z0-9-]+):(?P<region>[a-z0-9-]*):"
    r"(?P<account>\d{12})?:(?P<resource>[^\s\"',;)\]}]+)"
)
_AWS_ID = re.compile(r"\b(?P<prefix>AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|AGPA|APKA)[A-Z0-9]{16,17}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:amazonaws\.com(?:\.cn)?|compute\.internal)\b")
_IPV4 = re.compile(
    r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
    r"(?![\w.])"
)
_IPV6 = re.compile(
    r"(?<![\w:])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}:){0,5}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"
    r"|::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}"
    r")(?![\w:])"
)
#: 12 digits not inside a longer token or a decimal number (a sentence-final "." is fine).
_ACCOUNT = re.compile(r"(?<![\w-])(?<!\d\.)\d{12}(?![\w-])(?!\.\d)")
_RESOURCE_ID = re.compile(
    r"\b(?P<prefix>alb|tg|ecs/service|rds|lambda/function|ec2/instance|sqs/queue|sg|iam/role|"
    r"s3/bucket)/(?P<name>[A-Za-z0-9._+=@-]+)"
)
#: Structured keys whose whole value identifies a principal.
PRINCIPAL_KEYS = frozenset(
    {"userIdentity", "principalId", "userName", "userArn", "invokedBy", "sessionIssuer"}
)
_IP_KEYS = frozenset({"sourceIPAddress", "sourceIp", "clientIp", "ipAddress"})
_PRINCIPAL_ARN_TYPES = ("user/", "role/", "assumed-role/", "federated-user/", "group/", "root")


class RedactionError(RuntimeError):
    """Raised in strict mode when outgoing text still contains raw identifiers."""


class Redactor:
    def __init__(self, settings: RedactionSettings | None = None):
        self.settings = settings or RedactionSettings()
        self._forward: dict[tuple[str, str], str] = {}  # (category, raw) -> token
        self._reverse: dict[str, str] = {}  # token -> raw
        self._counts: dict[str, int] = {}

    # ------------------------------------------------------------------ mapping
    def _token(self, category: str, raw: str) -> str:
        key = (category, raw)
        if key not in self._forward:
            self._counts[category] = self._counts.get(category, 0) + 1
            token = f"{category}_{self._counts[category]}"
            self._forward[key] = token
            self._reverse[token] = raw
        return self._forward[key]

    @property
    def mapping(self) -> dict[str, str]:
        """token -> raw value (secrets included; never send this anywhere)."""
        return dict(self._reverse)

    @property
    def raw_values(self) -> set[str]:
        return {raw for (_, raw) in self._forward}

    # ------------------------------------------------------------------ text
    def _arn(self, m: re.Match) -> str:
        raw = m.group(0)
        if (key := ("ARN", raw)) in self._forward:
            return self._forward[key]
        account = m.group("account")
        acct = self._token("ACCOUNT", account) if account else ""
        resource = m.group("resource")
        principal = m.group("service") in ("iam", "sts") and resource.startswith(
            _PRINCIPAL_ARN_TYPES
        )
        kind, _, _ = resource.partition("/")
        prefix = f"{kind}/" if "/" in resource and kind.isalpha() and len(kind) <= 24 else ""
        res = self._token("PRINCIPAL" if principal else "RESOURCE", resource)
        out = f"arn:{m.group('partition')}:{m.group('service')}:{m.group('region')}:{acct}:"
        out += f"{prefix}{res}"
        self._forward[key] = out
        self._reverse.setdefault(out, raw)
        return out

    def redact(self, text: str) -> str:
        if not self.settings.enabled or not text:
            return text
        st = self.settings
        if st.secrets:
            text = _PEM.sub(lambda m: self._token("SECRET", m.group(0)), text)
            text = _JWT.sub(lambda m: self._token("SECRET", m.group(0)), text)
            for pattern in (_URL_PASSWORD, _BEARER, _SECRET_KV):
                text = pattern.sub(self._kv, text)
        if st.arns:
            text = _ARN.sub(self._arn, text)
        if st.principals:
            text = _AWS_ID.sub(
                lambda m: self._token(
                    "ACCESS_KEY" if m.group("prefix") in ("AKIA", "ASIA") else "PRINCIPAL",
                    m.group(0),
                ),
                text,
            )
            text = _EMAIL.sub(lambda m: self._token("EMAIL", m.group(0)), text)
        if st.hostnames:
            text = _HOST.sub(lambda m: self._token("HOST", m.group(0)), text)
        if st.ips:
            text = _IPV6.sub(self._ipv6, text)
            text = _IPV4.sub(lambda m: self._token("IP", m.group(0)), text)
        if st.account_ids:
            text = _ACCOUNT.sub(lambda m: self._token("ACCOUNT", m.group(0)), text)
        if st.resource_names:
            text = _RESOURCE_ID.sub(
                lambda m: (
                    f"{m.group('prefix')}/{self._token('RESOURCE', m.group('name'))}"
                    if not TOKEN.fullmatch(m.group("name"))
                    else m.group(0)
                ),
                text,
            )
        return text

    def _kv(self, m: re.Match) -> str:
        value = m.group("value")
        if TOKEN.fullmatch(value):
            return m.group(0)
        return f"{m.group('key')}{m.group('sep')}{self._token('SECRET', value)}"

    def _ipv6(self, m: re.Match) -> str:
        raw = m.group(0)
        # Times like 12:00:00 are not addresses; a real IPv6 has >= 4 groups or a "::".
        if "::" not in raw and raw.count(":") < 3:
            return raw
        return self._token("IP", raw)

    # ------------------------------------------------------------------ structured data
    def redact_obj(self, obj: Any, key: str | None = None) -> Any:
        """Redact strings anywhere in dicts/lists; identity keys are pseudonymised whole."""
        if not self.settings.enabled:
            return obj
        if isinstance(obj, dict):
            return {k: self.redact_obj(v, k) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact_obj(v, key) for v in obj]
        if isinstance(obj, str):
            if key in PRINCIPAL_KEYS and self.settings.principals and not TOKEN.search(obj):
                redacted = self.redact(obj)
                return redacted if redacted != obj else self._token("PRINCIPAL", obj)
            if key in _IP_KEYS and self.settings.ips:
                redacted = self.redact(obj)
                return redacted if redacted != obj else self._token("IP", obj)
            return self.redact(obj)
        return obj

    # ------------------------------------------------------------------ restore / check
    def restore(self, text: str) -> str:
        """Map pseudonyms back to raw values. Secrets stay redacted."""
        if not self._reverse:
            return text
        for token in sorted(self._reverse, key=len, reverse=True):
            raw = self._reverse[token]
            if not token.startswith("SECRET_"):
                text = text.replace(token, raw)
        return text

    def restore_obj(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.restore_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.restore_obj(v) for v in obj]
        if isinstance(obj, str):
            return self.restore(obj)
        return obj

    def check(self, text: str) -> list[str]:
        """Problems that would make sending `text` unsafe (empty list = safe)."""
        if not self.settings.enabled:
            return []
        problems = []
        for category, raw in self._forward:
            # Short plain words (e.g. an ARN resource "root") also occur in normal prose, so
            # only distinctive raw values are searched for; they were replaced either way.
            distinctive = len(raw) >= 10 or (len(raw) >= 6 and not raw.isalpha())
            if distinctive and raw in text:
                problems.append(f"raw {category.lower()} value still present")
        if Redactor(self.settings).redact(text) != text:
            problems.append("text contains identifiers that were not redacted")
        return sorted(set(problems))

    def ensure_safe(self, text: str) -> str:
        if self.settings.strict and (problems := self.check(text)):
            raise RedactionError("; ".join(problems))
        return text


class RedactingLLMClient(LLMClient):
    """Wraps an external client: redacts the request, restores identifiers in the response."""

    is_external = True

    def __init__(self, inner: LLMClient, settings: RedactionSettings | None = None):
        self.inner = inner
        self.settings = settings or RedactionSettings()
        self.last_redactor: Redactor | None = None

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    def complete(self, request: LLMRequest, redactor: Redactor | None = None) -> LLMResponse:
        """`redactor` lets a caller keep one pseudonym mapping across several calls (e.g.
        self-consistency samples or a repair attempt for the same incident)."""
        r = redactor or Redactor(self.settings)
        self.last_redactor = r
        outgoing = request.model_copy(
            update={"system": r.redact(request.system), "prompt": r.redact(request.prompt)}
        )
        r.ensure_safe(outgoing.system + "\n" + outgoing.prompt)
        response = self.inner.complete(outgoing)
        return response.model_copy(update={"text": r.restore(response.text)})


def guard_llm_client(client: LLMClient, settings: RedactionSettings) -> LLMClient:
    """The only way the system should obtain an LLM client: external clients are wrapped
    whenever redaction is enabled; local ones (the stub) are returned unchanged."""
    if settings.enabled and getattr(client, "is_external", True):
        return RedactingLLMClient(client, settings)
    return client


def redact_texts(texts: list[str], settings: RedactionSettings) -> tuple[list[str], Redactor]:
    r = Redactor(settings)
    out = [r.redact(t) for t in texts]
    for t in out:
        r.ensure_safe(t)
    return out, r


__all__ = [
    "PRINCIPAL_KEYS",
    "TOKEN",
    "RedactingLLMClient",
    "RedactionError",
    "Redactor",
    "guard_llm_client",
    "redact_texts",
]
