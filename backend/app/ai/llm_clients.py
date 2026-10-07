"""LLM clients: OpenAI-compatible and Gemini (external), and the deterministic stub (LOCAL-ONLY).

External clients can only be created through `build_llm_client` / `app.ai.clients`, which wraps
them in redaction (D67). The stub reads nothing but the prompt it is given: it parses the
evidence lines, scores taxonomy labels with fixed keyword rules, and writes a schema-valid
diagnosis that cites real evidence ids. It is a test double for the pipeline, NOT a baseline
or a result; everything it produces is labelled smoke-test / synthetic (DECISIONS D75).
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from collections.abc import Callable

from app.ai.http import Post, http_post, post_with_retries
from app.config import LLMSettings, Settings
from app.interfaces.llm_client import LLMClient, LLMRequest, LLMResponse


class LLMCallError(RuntimeError):
    """The provider answered, but not with a usable completion."""


def _cost(settings: LLMSettings, prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        prompt_tokens / 1000 * settings.input_cost_per_1k
        + completion_tokens / 1000 * settings.output_cost_per_1k,
        6,
    )


class OpenAICompatibleClient(LLMClient):
    """POST {base_url}/chat/completions (OpenAI, Azure-style gateways, vLLM, Ollama, ...)."""

    is_external = True

    def __init__(
        self,
        settings: LLMSettings,
        post: Post | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if settings.api_key is None and settings.base_url is None:
            raise ValueError("set CLOUDDIAG_LLM__API_KEY (or a base_url for a local server)")
        self.settings = settings
        self._post = post or http_post
        self._sleep = sleep

    @property
    def model_name(self) -> str:
        return self.settings.model

    def complete(self, request: LLMRequest) -> LLMResponse:
        st = self.settings
        base = (st.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {}
        if st.api_key is not None:
            headers["Authorization"] = f"Bearer {st.api_key.get_secret_value()}"
        body: dict = {
            "model": st.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        data = post_with_retries(
            self._post,
            f"{base}/chat/completions",
            headers,
            body,
            st.timeout_seconds,
            st.max_retries,
            self._sleep,
        )
        latency = (time.perf_counter() - t0) * 1000
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMCallError(f"unexpected response shape: {str(data)[:200]}") from e
        usage = data.get("usage") or {}
        pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        return LLMResponse(
            text=text,
            model=data.get("model", st.model),
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=round(latency, 1),
            estimated_cost_usd=_cost(st, pt, ct),
            extra={"finish_reason": data["choices"][0].get("finish_reason")},
        )


class GeminiClient(LLMClient):
    """POST {base_url}/models/{model}:generateContent (Google Gemini API)."""

    is_external = True

    def __init__(
        self,
        settings: LLMSettings,
        post: Post | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if settings.api_key is None:
            raise ValueError("set CLOUDDIAG_LLM__API_KEY for Gemini")
        self.settings = settings
        self._post = post or http_post
        self._sleep = sleep

    @property
    def model_name(self) -> str:
        return self.settings.model

    def complete(self, request: LLMRequest) -> LLMResponse:
        st = self.settings
        base = (st.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        model = st.model if st.model.startswith("models/") else f"models/{st.model}"
        config: dict = {"temperature": request.temperature, "maxOutputTokens": request.max_tokens}
        if request.seed is not None:
            config["seed"] = request.seed
        if request.json_mode:
            config["responseMimeType"] = "application/json"
        body = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": config,
        }
        t0 = time.perf_counter()
        data = post_with_retries(
            self._post,
            f"{base}/{model}:generateContent",
            {"x-goog-api-key": st.api_key.get_secret_value()},
            body,
            st.timeout_seconds,
            st.max_retries,
            self._sleep,
        )
        latency = (time.perf_counter() - t0) * 1000
        try:
            cand = data["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMCallError(f"unexpected response shape: {str(data)[:200]}") from e
        usage = data.get("usageMetadata") or {}
        pt = int(usage.get("promptTokenCount", 0))
        ct = int(usage.get("candidatesTokenCount", 0))
        return LLMResponse(
            text=text,
            model=data.get("modelVersion", st.model),
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=round(latency, 1),
            estimated_cost_usd=_cost(st, pt, ct),
            extra={"finish_reason": cand.get("finishReason")},
        )


# ----------------------------------------------------------------------------- stub (LOCAL-ONLY)
_EVIDENCE = re.compile(
    r"^\[(?P<evd>evd_[0-9a-f]{16})\] (?P<time>\S+) (?P<resource>\S+) (?P<tag>[A-Z]+) (?P<text>.*)$"
)
_HISTORICAL = re.compile(
    r"^\[(?P<id>[\w-]+)\] \(HISTORICAL, similarity [\d.]+, past label (?P<label>[a-z_]+)\)"
)
_AFFECTED = re.compile(r"^Affected resources: (?P<r>\S+?)(?:,|$)")
_TITLE = re.compile(r"^Title: (?P<t>.*)$")

#: (label, pattern, weight): fixed keyword rules over the evidence lines of the prompt.
STUB_RULES: tuple[tuple[str, str, float], ...] = (
    (
        "deployment_failure",
        r"\b(UpdateService|RegisterTaskDefinition|UpdateFunctionCode\w*|"
        r"CreateDeployment)\b.*(task definition|code package|revision|deployed)",
        3.0,
    ),
    ("deployment_failure", r"Failed to start application|returned 503", 2.0),
    ("connection_exhaustion", r"too many clients|remaining connection slots|connection pool", 3.0),
    ("connection_exhaustion", r"DatabaseConnections=", 1.5),
    ("connection_exhaustion", r"desiredCount of .* changed", 1.0),
    ("function_timeout", r"Task timed out", 3.0),
    ("function_timeout", r"Timeout of .* changed|Timeout \d+ -> \d+", 2.0),
    ("cpu_saturation", r"\bCPUUtilization=.*anomalous", 1.5),
    ("cpu_saturation", r"Worker pool saturated|queue depth", 2.0),
    ("lb_error_spike", r"HTTPCode_ELB_5XX_Count=.*anomalous", 2.0),
    ("lb_error_spike", r"elb_status_code=503|ModifyListener|Listener on", 2.0),
    ("iam_permission_failure", r"AccessDenied|not authorized", 3.0),
    ("iam_permission_failure", r"DetachRolePolicy|attached policies changed", 2.0),
    ("security_group_misconfiguration", r"RevokeSecurityGroupIngress|ingress rules changed", 3.0),
    ("security_group_misconfiguration", r"Connection timed out", 1.5),
    ("db_latency_increase", r"\b(ReadLatency|WriteLatency|DiskQueueDepth)=.*anomalous", 1.5),
    ("db_latency_increase", r"Slow query|duration: \d+ ms", 2.0),
    ("db_latency_increase", r"ModifyDBInstance|StorageThroughput", 2.0),
    ("task_crash_loop", r"OutOfMemory|Container killed|exit code 137", 3.0),
    ("task_crash_loop", r"RunningTaskCount=.*anomalous|UnHealthyHostCount=.*anomalous", 1.0),
    (
        "queue_backlog",
        r"(ApproximateAgeOfOldestMessage|ApproximateNumberOfMessagesVisible)="
        r".*anomalous",
        1.5,
    ),
    ("queue_backlog", r"TooManyRequestsException|reserved concurrency|PutFunctionConcurrency", 3.0),
)
_COMPILED = tuple((lab, re.compile(p, re.IGNORECASE), w) for lab, p, w in STUB_RULES)
_ACTIONS = {
    "deployment_failure": "Roll back to the previous release and verify startup configuration.",
    "connection_exhaustion": "Reduce connection demand (pooling, max connections) and scale back.",
    "function_timeout": "Restore the function timeout and check downstream latency.",
    "cpu_saturation": "Scale out the service and investigate the CPU-heavy workload.",
    "lb_error_spike": "Restore the listener / target configuration of the load balancer.",
    "iam_permission_failure": "Re-attach the removed policy to the execution role.",
    "security_group_misconfiguration": "Restore the removed security group ingress rule.",
    "db_latency_increase": "Restore database storage performance and review slow queries.",
    "task_crash_loop": "Restore task memory limits and stop the crash loop.",
    "queue_backlog": "Restore consumer concurrency and drain the backlog.",
}


def _trim(text: str, n: int = 160) -> str:
    """Cut at a word boundary so no quoted number is truncated."""
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut + " ..."


class StubLLM(LLMClient):
    """LOCAL-ONLY deterministic stand-in. `script` replays fixed outputs first (tests)."""

    is_external = False
    MODEL = "stub-deterministic-v1"

    def __init__(self, script: list[str] | None = None):
        self.script = list(script or [])
        self.calls: list[LLMRequest] = []

    @property
    def model_name(self) -> str:
        return self.MODEL

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        text = self.script.pop(0) if self.script else json.dumps(self._diagnose(request))
        pt = math.ceil(len(request.system + request.prompt) / 4)
        return LLMResponse(
            text=text,
            model=self.MODEL,
            prompt_tokens=pt,
            completion_tokens=math.ceil(len(text) / 4),
            latency_ms=0.0,
            estimated_cost_usd=0.0,
            extra={"local_only": True},
        )

    def _diagnose(self, request: LLMRequest) -> dict:
        lines, historical, affected, title = [], [], None, "Incident"
        context = request.prompt.split("================")[1] if "====" in request.prompt else ""
        for raw in context.splitlines():
            line = raw.strip()
            if m := _EVIDENCE.match(line):
                lines.append(m.groupdict())
            elif m := _HISTORICAL.match(line):
                historical.append(m.groupdict())
            elif (m := _AFFECTED.match(line)) and affected is None:
                affected = m.group("r")
            elif m := _TITLE.match(line):
                title = m.group("t")
            elif line.startswith("Candidate cause"):
                lines.append(
                    {"evd": None, "time": "", "resource": "", "tag": "CAUSE", "text": line}
                )
        scores: dict[str, float] = {}
        contrib: dict[str, list[tuple[float, dict]]] = {}
        for i, ln in enumerate(lines):
            decay = 1.0 / (1.0 + 0.05 * i) * (1.5 if ln["tag"] == "CAUSE" else 1.0)
            for label, pattern, w in _COMPILED:
                if pattern.search(ln["text"]):
                    scores[label] = scores.get(label, 0.0) + w * decay
                    if ln["evd"]:
                        contrib.setdefault(label, []).append((w * decay, ln))
        if request.temperature > 0 and scores:
            rng = random.Random(f"{request.seed}:{len(request.prompt)}")
            scores = {
                k: v * math.exp(request.temperature * rng.gauss(0, 0.35))
                for k, v in sorted(scores.items())
            }
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        evidence_lines = [ln for ln in lines if ln["evd"]]
        resource = affected or (evidence_lines[0]["resource"] if evidence_lines else "unknown")
        if not ranked or ranked[0][1] < 2.0 or len(evidence_lines) < 3:
            return self._insufficient(title, resource, evidence_lines)

        best_label, best_score = ranked[0]
        total = sum(v for _, v in ranked)
        conf = round(min(0.9, max(0.35, best_score / total)), 2)
        support = sorted(contrib.get(best_label, []), key=lambda x: -x[0])
        cited, seen = [], set()
        for _, ln in support:
            if ln["evd"] not in seen:
                seen.add(ln["evd"])
                cited.append(ln)
            if len(cited) == 3:
                break
        changes = [ln for ln in cited if ln["tag"] in ("CLOUDTRAIL", "CONFIG")]
        root_resource = (changes or cited)[0]["resource"]
        alts = []
        rest = ranked[1:3]
        rest_total = sum(v for _, v in rest) or 1.0
        for label, v in rest:
            c = math.floor((1 - conf) * 0.95 * v / rest_total * 100) / 100
            alts.append(
                {
                    "taxonomy_label": label,
                    "description": f"Signals also match {label.replace('_', ' ')}.",
                    "confidence": c,
                    "rejected_because": f"weaker support than {best_label.replace('_', ' ')}",
                    "label": "HYPOTHESIS",
                }
            )
        alts.sort(key=lambda a: -a["confidence"])
        same = [h for h in historical if h["label"] == best_label]
        influence = {"used": False, "incident_ids": [], "how": ""}
        if same:
            influence = {
                "used": True,
                "incident_ids": [same[0]["id"]],
                "how": (
                    f"Past incident {same[0]['id']} showed the same pattern; the current evidence "
                    f"{cited[0]['evd']} supports it independently."
                ),
            }
        return {
            "incident_summary": f"{title}. Stub diagnosis (LOCAL-ONLY, smoke-test / synthetic).",
            "root_cause": {
                "taxonomy_label": best_label,
                "description": (
                    f"{best_label.replace('_', ' ').capitalize()} on {root_resource}, indicated by "
                    f"{len(cited)} cited signal(s)."
                ),
                "confidence": conf,
                "resource_id": root_resource,
                "label": "INFERENCE",
            },
            "supporting_evidence": [
                {
                    "evidence_id": ln["evd"],
                    "explanation": f"{ln['tag']} on {ln['resource']}: {_trim(ln['text'])}",
                    "label": "FACT",
                }
                for ln in cited
            ],
            "contradicting_evidence": [],
            "contributing_factors": [],
            "alternative_hypotheses": alts,
            "historical_influence": influence,
            "impact_analysis": {
                "services": sorted({resource.split("/")[0], root_resource.split("/")[0]}),
                "resources": sorted({resource, root_resource}),
                "blast_radius": f"{resource} and everything routed through it",
                "user_impact": "Requests through the affected path fail or slow down.",
            },
            "severity_suggestion": "HIGH" if conf >= 0.6 else "MEDIUM",
            "recommendations": [
                {
                    "category": "IMMEDIATE",
                    "action": _ACTIONS.get(best_label, "Mitigate the failing component."),
                    "label": "RECOMMENDATION",
                },
                {
                    "category": "INVESTIGATIVE",
                    "action": f"Confirm the cited signals on {root_resource}.",
                    "label": "RECOMMENDATION",
                },
            ],
            "missing_information": [],
            "requires_human_review": conf < 0.6,
        }

    @staticmethod
    def _insufficient(title: str, resource: str, evidence_lines: list[dict]) -> dict:
        return {
            "incident_summary": f"{title}. Stub diagnosis (LOCAL-ONLY, smoke-test / synthetic).",
            "root_cause": {
                "taxonomy_label": "insufficient_evidence",
                "description": "The context does not contain enough evidence for a root cause.",
                "confidence": 0.3,
                "resource_id": resource,
                "label": "INFERENCE",
            },
            "supporting_evidence": [
                {
                    "evidence_id": ln["evd"],
                    "explanation": f"{ln['tag']} on {ln['resource']} (symptom only)",
                    "label": "FACT",
                }
                for ln in evidence_lines[:1]
            ],
            "contradicting_evidence": [],
            "contributing_factors": [],
            "alternative_hypotheses": [],
            "historical_influence": {"used": False, "incident_ids": [], "how": ""},
            "impact_analysis": {
                "services": [],
                "resources": [resource],
                "blast_radius": "",
                "user_impact": "unknown",
            },
            "severity_suggestion": "MEDIUM",
            "recommendations": [
                {
                    "category": "INVESTIGATIVE",
                    "action": "Collect application logs, CloudTrail "
                    "and dependency metrics, then re-diagnose.",
                    "label": "RECOMMENDATION",
                }
            ],
            "missing_information": ["application logs", "CloudTrail events", "dependency metrics"],
            "requires_human_review": True,
        }


def build_llm_client(settings: Settings, post: Post | None = None) -> LLMClient:
    """LLM client for `settings.llm.provider`, created through the wrapping factory."""
    from app.ai.clients import create_llm_client

    provider = settings.llm.provider
    if provider == "stub":
        return create_llm_client(StubLLM, settings)
    if provider == "openai_compatible":
        return create_llm_client(OpenAICompatibleClient, settings, settings.llm, post)
    if provider == "gemini":
        return create_llm_client(GeminiClient, settings, settings.llm, post)
    raise ValueError(f"unknown llm provider {provider!r}")
