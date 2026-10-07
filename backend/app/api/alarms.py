"""CloudWatch alarm ingestion: SNS notifications and EventBridge events -> incidents.

Accepted bodies for `POST /api/incidents` (besides the native JSON form):

  SNS        {"Type": "Notification", "TopicArn": ..., "Message": "<CloudWatch alarm JSON>",
              "Signature": ..., "SigningCertURL": ..., "SignatureVersion": "1"|"2", ...}
  EventBridge {"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change",
              "detail": {"alarmName", "state": {...}, "previousState": {...},
                         "configuration": {"metrics": [...]}}, ...}

Only transitions INTO the ALARM state open an incident; OK / INSUFFICIENT_DATA notifications
are acknowledged and ignored. SNS messages are signature-checked (SignatureVersion 1 = SHA1,
2 = SHA256, RSA PKCS#1 v1.5) against a certificate that may only be fetched over HTTPS from
`sns.<region>.amazonaws.com`; SubscriptionConfirmation messages are verified but never
auto-confirmed (DECISIONS D95).
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

_CERT_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_SIGNED_NOTIFICATION = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SIGNED_SUBSCRIPTION = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._+=@-]")


class AlarmError(ValueError):
    """The payload is not a usable CloudWatch alarm notification."""


class SignatureError(ValueError):
    """The SNS message signature is missing or invalid."""


class Alarm(BaseModel):
    alarm_name: str
    state: str
    previous_state: str | None = None
    reason: str = ""
    time: datetime
    region: str | None = None
    account: str | None = None
    namespace: str | None = None
    metric: str | None = None
    dimensions: dict[str, str] = Field(default_factory=dict)
    threshold: float | None = None
    topic_arn: str | None = None
    via: str  # sns | eventbridge


# ----------------------------------------------------------------------------- dimensions
def _canon(name: str) -> str:
    return _SAFE_NAME.sub("-", name)[:200]


def resources_from_dimensions(namespace: str | None, dims: dict[str, str]) -> list[str]:
    """Canonical resource ids (D32) for the dimensions CloudWatch puts on the alarm."""
    out: list[str] = []
    for key, value in dims.items():
        if key == "LoadBalancer":  # app/<name>/<id>
            parts = value.split("/")
            out.append(f"alb/{_canon(parts[1] if len(parts) >= 2 else value)}")
        elif key == "TargetGroup":
            parts = value.split("/")
            out.append(f"tg/{_canon(parts[1] if len(parts) >= 2 else value)}")
        elif key == "DBInstanceIdentifier":
            out.append(f"rds/{_canon(value)}")
        elif key == "FunctionName":
            out.append(f"lambda/function/{_canon(value)}")
        elif key == "ServiceName" and (namespace or "").startswith(("AWS/ECS", "ECS/")):
            out.append(f"ecs/service/{_canon(value)}")
        elif key == "QueueName":
            out.append(f"sqs/queue/{_canon(value)}")
        elif key == "InstanceId":
            out.append(f"ec2/instance/{_canon(value)}")
    return list(dict.fromkeys(out))


# ----------------------------------------------------------------------------- parsing
def _ts(value: str | None) -> datetime:
    if not value:
        raise AlarmError("missing alarm timestamp")
    v = value.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", v):  # 2026-10-07T12:00:00.000+0000
        v = v[:-2] + ":" + v[-2:]
    try:
        t = datetime.fromisoformat(v)
    except ValueError as e:
        raise AlarmError(f"bad timestamp {value!r}") from e
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _region_from_arn(arn: str | None) -> str | None:
    parts = (arn or "").split(":")
    return parts[3] if len(parts) > 4 and re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", parts[3]) else None


def _from_cloudwatch_message(msg: dict, topic: str | None) -> Alarm:
    trig = msg.get("Trigger") or {}
    dims = {}
    for d in trig.get("Dimensions") or []:
        name = d.get("name") or d.get("Name")
        if name:
            dims[name] = str(d.get("value") or d.get("Value") or "")
    try:
        threshold = float(trig["Threshold"]) if "Threshold" in trig else None
    except (TypeError, ValueError):
        threshold = None
    if not msg.get("AlarmName") or not msg.get("NewStateValue"):
        raise AlarmError("SNS message is not a CloudWatch alarm notification")
    return Alarm(
        alarm_name=str(msg["AlarmName"])[:255],
        state=str(msg["NewStateValue"]),
        previous_state=msg.get("OldStateValue"),
        reason=str(msg.get("NewStateReason", ""))[:2000],
        time=_ts(msg.get("StateChangeTime")),
        # "Region" is a display name ("US East (N. Virginia)"); the code is in the ARNs.
        region=_region_from_arn(msg.get("AlarmArn")) or _region_from_arn(topic),
        account=str(msg.get("AWSAccountId") or "") or None,
        namespace=trig.get("Namespace"),
        metric=trig.get("MetricName"),
        dimensions=dims,
        threshold=threshold,
        topic_arn=topic,
        via="sns",
    )


def _from_eventbridge(ev: dict) -> Alarm:
    detail = ev.get("detail") or {}
    if not detail.get("alarmName"):
        raise AlarmError("EventBridge event has no detail.alarmName")
    state = detail.get("state") or {}
    metric, namespace, dims = None, None, {}
    for m in (detail.get("configuration") or {}).get("metrics") or []:
        stat = (m.get("metricStat") or {}).get("metric") or {}
        if stat:
            metric, namespace = stat.get("name"), stat.get("namespace")
            dims = {str(k): str(v) for k, v in (stat.get("dimensions") or {}).items()}
            break
    return Alarm(
        alarm_name=str(detail["alarmName"])[:255],
        state=str(state.get("value", "")),
        previous_state=(detail.get("previousState") or {}).get("value"),
        reason=str(state.get("reason", ""))[:2000],
        time=_ts(state.get("timestamp") or ev.get("time")),
        region=ev.get("region"),
        account=ev.get("account"),
        namespace=namespace,
        metric=metric,
        dimensions=dims,
        via="eventbridge",
    )


def kind_of(body: dict) -> str:
    """native | sns-notification | sns-subscription | eventbridge"""
    t = body.get("Type")
    if t == "Notification" and "TopicArn" in body:
        return "sns-notification"
    if t in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        return "sns-subscription"
    if body.get("source") == "aws.cloudwatch" and "detail" in body:
        return "eventbridge"
    return "native"


def parse_alarm(body: dict) -> Alarm:
    kind = kind_of(body)
    if kind == "sns-notification":
        try:
            msg = json.loads(body.get("Message") or "")
        except json.JSONDecodeError as e:
            raise AlarmError("SNS Message is not JSON") from e
        if not isinstance(msg, dict):
            raise AlarmError("SNS Message is not a JSON object")
        return _from_cloudwatch_message(msg, body.get("TopicArn"))
    if kind == "eventbridge":
        if body.get("detail-type") != "CloudWatch Alarm State Change":
            raise AlarmError(f"unsupported EventBridge detail-type {body.get('detail-type')!r}")
        return _from_eventbridge(body)
    raise AlarmError("not an SNS or EventBridge alarm payload")


# ----------------------------------------------------------------------------- SNS signatures
FetchCert = Callable[[str], bytes]


def _default_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (host checked first)
        return r.read()


class SnsVerifier:
    def __init__(self, fetch: FetchCert | None = None):
        self.fetch = fetch or _default_fetch
        self._cache: dict[str, object] = {}

    @staticmethod
    def check_cert_url(url: str) -> None:
        u = urllib.parse.urlparse(url or "")
        if (
            u.scheme != "https"
            or not _CERT_HOST.match(u.hostname or "")
            or not u.path.endswith(".pem")
        ):
            raise SignatureError("SigningCertURL must be https://sns.<region>.amazonaws.com/*.pem")

    @staticmethod
    def string_to_sign(msg: dict) -> bytes:
        keys = _SIGNED_NOTIFICATION if msg.get("Type") == "Notification" else _SIGNED_SUBSCRIPTION
        parts = []
        for k in keys:
            if k in msg and msg[k] is not None:
                parts.append(f"{k}\n{msg[k]}\n")
        return "".join(parts).encode()

    def _public_key(self, url: str):
        from cryptography import x509

        if url not in self._cache:
            self._cache[url] = x509.load_pem_x509_certificate(self.fetch(url)).public_key()
        return self._cache[url]

    def verify(self, msg: dict) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        version = str(msg.get("SignatureVersion", ""))
        if version not in ("1", "2"):
            raise SignatureError("unsupported or missing SignatureVersion")
        url = msg.get("SigningCertURL") or msg.get("SigningCertUrl") or ""
        self.check_cert_url(url)
        try:
            signature = base64.b64decode(msg.get("Signature") or "", validate=True)
        except ValueError as e:
            raise SignatureError("Signature is not base64") from e
        algo = hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303 (SNS v1 spec)
        try:
            self._public_key(url).verify(
                signature, self.string_to_sign(msg), padding.PKCS1v15(), algo
            )
        except InvalidSignature as e:
            raise SignatureError("SNS signature does not verify") from e
        except Exception as e:  # noqa: BLE001 - certificate fetch/parse failures
            raise SignatureError(f"could not verify SNS signature: {type(e).__name__}") from e
