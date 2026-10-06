"""Seeded, deterministic incident simulator.

Determinism: every random draw comes from ``random.Random`` seeded with ``"{seed}:{scenario_key}"``
(stable across Python versions and platforms), there is no wall-clock input, and values are
rounded before serialisation. Same seed + same generator version => byte-identical output.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.contracts.events import CanonicalEvent, EventSource, Severity
from app.contracts.ground_truth import GroundTruth
from app.contracts.taxonomy import RootCause
from app.offline.faults import SYMPTOMS, Effect, FaultSignature, signature
from app.offline.models import (
    AnomalyLabel,
    Category,
    IncidentRecord,
    InjectedFault,
    ObservableScenario,
    ResourceRecord,
    ScenarioMeta,
    TruthRecord,
)
from app.offline.topology import ACCOUNT_ID, REGION, MetricSpec, Topology, build_topology

GENERATOR_VERSION = "1.0.0"
BASE_TIME = datetime(2025, 3, 1, tzinfo=UTC)
PRE_ONSET = timedelta(minutes=60)
POST_ONSET = timedelta(minutes=30)
_LEVEL = {
    "INFO": Severity.INFO,
    "WARNING": Severity.WARNING,
    "ERROR": Severity.ERROR,
    "CRITICAL": Severity.CRITICAL,
}


@dataclass(frozen=True)
class ScenarioSpec:
    key: str
    category: Category
    faults: tuple[RootCause, ...]
    topology: str
    variant: int
    degradation: str | None = None  # insufficient-evidence modes: alarm_only | sparse
    red_herring: bool = False


@dataclass
class _FaultInstance:
    sig: FaultSignature
    onset: datetime
    visible: bool


@dataclass
class _Build:
    events: list[CanonicalEvent] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    red_herrings: list[str] = field(default_factory=list)


def incident_id_for(seed: int, key: str) -> str:
    return "inc-" + hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:10]


def _r(x: float) -> float:
    return round(x, 4) + 0.0  # + 0.0 normalises -0.0


def _fraction(e: Effect, t: datetime, onset: datetime) -> float:
    dt = (t - onset).total_seconds() - e.lag_s
    if dt < 0:
        return 0.0
    if e.shape == "oscillate":
        return 0.5 + 0.5 * math.cos(2 * math.pi * dt / 240.0)
    return 1.0 if e.ramp_s <= 0 else min(1.0, dt / e.ramp_s)


class _Simulator:
    def __init__(self, spec: ScenarioSpec, seed: int):
        self.spec = spec
        self.seed = seed
        self.rng = random.Random(f"{seed}:{spec.key}")
        self.incident_id = incident_id_for(seed, spec.key)
        self.out = _Build()
        self._seq = 0

    # ---------------------------------------------------------------- parameters
    def _draw_parameters(self) -> None:
        rng, spec = self.rng, self.spec
        self.topo: Topology = build_topology(spec.topology, rng)
        day = rng.randrange(0, 120)
        minute = rng.randrange(0, 24 * 60)
        self.onset = BASE_TIME + timedelta(days=day, minutes=minute)
        self.severity = round(rng.uniform(0.55, 1.0), 3)
        self.noise = rng.choice([0.7, 1.0, 1.5, 2.0])
        self.period = 60 if spec.variant % 2 == 0 else 300
        self.missing_rate = round(rng.uniform(0.0, 0.08), 3)
        self.detect_lag = rng.randrange(120, 420)
        self.window_start = self.onset - PRE_ONSET
        self.window_end = self.onset + POST_ONSET
        self.faults: list[_FaultInstance] = []
        for i, fault in enumerate(spec.faults):
            sig = signature(fault, self.topo)
            onset = (
                self.onset if i == 0 else self.onset + timedelta(seconds=rng.randrange(180, 420))
            )
            self.faults.append(_FaultInstance(sig, onset, rng.random() < sig.trigger_visible_prob))
        self.grid = []
        t = self.window_start
        while t <= self.window_end:
            self.grid.append(t)
            t += timedelta(seconds=self.period)
        self.ctx = {r: self.topo.rid(r) for r in self.topo.resources}
        self.ctx |= {f"{r}_name": self.topo.short(r) for r in self.topo.resources}
        self.ctx["actor"] = self.topo.rid(self.topo.actor)

    def _fmt(self, template: str) -> str:
        rng = self.rng
        return template.format_map(
            self.ctx
            | {
                "rev": rng.randrange(20, 90),
                "n": rng.randrange(51, 400),
                "ms": rng.randrange(900, 4800),
                "sess": f"{rng.getrandbits(32):08x}",
            }
        )

    def _ref(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}:{self.incident_id}:{self._seq:05d}"

    # ---------------------------------------------------------------- metrics
    def _metrics(self) -> dict[tuple[str, str], dict[datetime, float]]:
        rng = self.rng
        scale_of = {}
        series: dict[tuple[str, str], dict[datetime, float]] = {}
        self.fractions: dict[tuple[str, str], list[tuple[Effect, datetime]]] = {}
        for f in self.faults:
            for e in f.sig.effects:
                self.fractions.setdefault((e.role, e.metric), []).append((e, f.onset))
        for spec in self.topo.metrics:
            scale = self.period / 60 if spec.count else 1.0
            scale_of[(spec.role, spec.name)] = scale
            mean, sd = (
                spec.base * scale,
                spec.sd * self.noise * (math.sqrt(scale) if spec.count else 1),
            )
            phase = rng.uniform(0, 2 * math.pi)
            ar, values = 0.0, {}
            for t in self.grid:
                ar = 0.5 * ar + rng.gauss(0, sd) if sd > 0 else 0.0
                hours = (t - BASE_TIME).total_seconds() / 3600
                v = mean * (1 + 0.05 * math.sin(2 * math.pi * hours / 24 + phase)) + ar
                for e, onset in self.fractions.get((spec.role, spec.name), []):
                    delta = (e.peak * scale - mean) * self.severity
                    v += delta * _fraction(e, t, onset)
                values[t] = self._clip(spec, v, scale)
            series[(spec.role, spec.name)] = values
        self.scale_of = scale_of
        return series

    @staticmethod
    def _clip(spec: MetricSpec, v: float, scale: float) -> float:
        hi = spec.hi * scale if spec.hi is not None else None
        v = max(spec.lo * scale if spec.count else spec.lo, v)
        if hi is not None:
            v = min(hi, v)
        return float(round(v)) if (spec.integer or spec.count) else _r(v)

    def _drop_mask(self, spec: MetricSpec) -> set[datetime]:
        rng = self.rng
        dropped = {t for t in self.grid if rng.random() < self.missing_rate}
        if rng.random() < 0.3:  # a pre-onset collection gap
            gap_start = self.window_start + timedelta(minutes=rng.randrange(5, 40))
            gap_end = gap_start + timedelta(minutes=rng.randrange(3, 9))
            dropped |= {t for t in self.grid if gap_start <= t < gap_end}
        return dropped

    def _emit_metrics(self, series: dict[tuple[str, str], dict[datetime, float]]) -> None:
        for spec in self.topo.metrics:
            key = (spec.role, spec.name)
            dropped = self._drop_mask(spec)
            rid = self.topo.rid(spec.role)
            emitted: dict[datetime, CanonicalEvent] = {}
            for t, v in series[key].items():
                if t in dropped:
                    continue
                ev = CanonicalEvent.build(
                    timestamp=t,
                    source=EventSource.CLOUDWATCH_METRIC,
                    service=self.topo.service(spec.role),
                    resource_id=rid,
                    event_type="metric_datapoint",
                    metric=spec.name,
                    value=v,
                    metadata={
                        "namespace": spec.namespace,
                        "stat": spec.stat,
                        "period_seconds": self.period,
                        "unit": spec.unit,
                    },
                    raw_ref=f"cw:{spec.namespace}/{spec.name}/{rid}",
                )
                emitted[t] = ev
                self.out.events.append(ev)
            for e, onset in self.fractions.get(key, []):
                if not e.key:
                    continue
                hits = [
                    emitted[t] for t in self.grid if t in emitted and _fraction(e, t, onset) >= 0.5
                ]
                self.out.evidence += [h.event_id for h in hits[:2]]

    # ---------------------------------------------------------------- logs / trail / config
    def _log(self, role: str, t: datetime, level: str, msg: str) -> CanonicalEvent:
        group = self.topo.log_groups[role]
        return CanonicalEvent.build(
            timestamp=t,
            source=EventSource.CLOUDWATCH_LOG,
            service=self.topo.service(role),
            resource_id=self.topo.rid(role),
            event_type="log_line",
            severity=_LEVEL[level],
            message=msg,
            metadata={"log_group": group, "log_stream": f"{group}/stream-1"},
            raw_ref=self._ref("cwlogs"),
        )

    def _trail(
        self,
        role: str,
        t: datetime,
        name: str,
        source: str,
        msg: str,
        error_code: str | None = None,
        rid: str | None = None,
        service: str | None = None,
    ) -> CanonicalEvent:
        meta = {
            "eventSource": source,
            "awsRegion": REGION,
            "userIdentity": f"arn:aws:iam::{ACCOUNT_ID}:user/deployer",
        }
        if error_code:
            meta["errorCode"] = error_code
        return CanonicalEvent.build(
            timestamp=t,
            source=EventSource.CLOUDTRAIL,
            service=service or source.split(".")[0],
            resource_id=rid or self.topo.rid(role),
            event_type=name,
            severity=Severity.ERROR if error_code else Severity.INFO,
            message=msg,
            metadata=meta,
            raw_ref=self._ref("ct"),
        )

    def _config(
        self,
        role: str,
        t: datetime,
        msg: str,
        rid: str | None = None,
        rtype: str | None = None,
        service: str | None = None,
    ) -> CanonicalEvent:
        res = self.topo.resources.get(role)
        return CanonicalEvent.build(
            timestamp=t,
            source=EventSource.AWS_CONFIG,
            service=service or res.service,
            resource_id=rid or res.resource_id,
            event_type="ConfigurationItemChange",
            message=msg,
            metadata={"resourceType": rtype or res.resource_type, "configurationItemStatus": "OK"},
            raw_ref=self._ref("cfg"),
        )

    def _second(self, base: datetime, offset_s: float) -> datetime:
        t = base + timedelta(seconds=offset_s)
        return t.replace(microsecond=0)

    def _background(self) -> None:
        rng, topo = self.rng, self.topo
        span = (self.window_end - self.window_start).total_seconds()
        info = [
            "GET /api/orders 200 {ms}ms",
            "GET /healthz 200 3ms",
            "POST /api/cart 201 {ms}ms",
            "Processed batch of {n} records",
            "Cache refresh completed",
        ]
        warn = ["Slow request GET /api/search took {ms}ms", "Retrying downstream call (attempt 2)"]
        err = ["GET /favicon.ico 404", "Client closed connection before response was sent"]
        for role in topo.log_groups:
            if role in ("alb",):
                continue
            for _ in range(rng.randrange(15, 30)):
                t = self._second(self.window_start, rng.uniform(0, span))
                r = rng.random()
                level, pool = (
                    ("INFO", info)
                    if r < 0.8
                    else (("WARNING", warn) if r < 0.95 else ("ERROR", err))
                )
                self.out.events.append(self._log(role, t, level, self._fmt(rng.choice(pool))))
        benign = {
            "ecs": ("DescribeServices", "ecs.amazonaws.com"),
            "lambda": ("GetFunction20150331v2", "lambda.amazonaws.com"),
            "ec2": ("DescribeInstances", "ec2.amazonaws.com"),
            "rds": ("DescribeDBInstances", "rds.amazonaws.com"),
            "elasticloadbalancing": ("DescribeTargetHealth", "elasticloadbalancing.amazonaws.com"),
        }
        roles = ["app", "db", "alb"]
        for _ in range(rng.randrange(3, 9)):
            role = rng.choice(roles)
            name, src = benign[topo.service(role)]
            t = self._second(self.window_start, rng.uniform(0, span))
            self.out.events.append(self._trail(role, t, name, src, f"{name} (read-only)"))
        if rng.random() < 0.5:  # benign tag change well before onset
            t = self._second(self.window_start, rng.uniform(0, span / 3))
            self.out.events.append(
                self._config("db", t, "Tags updated on {}".format(topo.short("db")))
            )

    def _fault_signals(self, f: _FaultInstance) -> None:
        rng, sig = self.rng, f.sig
        for lg in sig.logs:
            n = max(3, round(lg.count * self.severity))
            first = True
            for i in range(n):
                t = self._second(f.onset, lg.lag_s + lg.spread_s * i / n + rng.uniform(0, 20))
                if t > self.window_end:
                    break
                ev = self._log(lg.role, t, lg.level, self._fmt(lg.template))
                self.out.events.append(ev)
                if lg.key and (first or i == 1):
                    self.out.evidence.append(ev.event_id)
                first = False
        for tr in sig.trail:
            if tr.optional and not f.visible:
                continue
            t = self._second(f.onset, tr.offset_s + rng.uniform(-5, 5))
            ev = self._trail(
                tr.role, t, tr.event_name, tr.event_source, self._fmt(tr.message), tr.error_code
            )
            self.out.events.append(ev)
            if tr.key:
                self.out.evidence.append(ev.event_id)
        for cf in sig.config:
            if cf.optional and not f.visible:
                continue
            t = self._second(f.onset, cf.offset_s + rng.uniform(0, 30))
            ev = self._config(cf.role, t, self._fmt(cf.message))
            self.out.events.append(ev)
            if cf.key:
                self.out.evidence.append(ev.event_id)

    def _red_herring(self) -> list[ResourceRecord]:
        """An unrelated deployment/config change near onset, on resources off the causal path."""
        rng = self.rng
        s = f"{rng.getrandbits(16):04x}"
        options = [
            (
                "ecs/service/batch-" + s,
                "AWS::ECS::Service",
                "ecs",
                "UpdateService",
                "ecs.amazonaws.com",
                "UpdateService: batch-{s} now uses task definition batch:{rev}",
            ),
            (
                "lambda/function/report-gen-" + s,
                "AWS::Lambda::Function",
                "lambda",
                "UpdateFunctionCode20150331v2",
                "lambda.amazonaws.com",
                "UpdateFunctionCode: new code package deployed to report-gen-{s}",
            ),
            (
                "sg/sg-" + f"{rng.getrandbits(32):08x}",
                "AWS::EC2::SecurityGroup",
                "ec2",
                "AuthorizeSecurityGroupIngress",
                "ec2.amazonaws.com",
                "AuthorizeSecurityGroupIngress: tcp/443 added to bastion security group",
            ),
        ]
        rid, rtype, service, name, src, msg = options[rng.randrange(len(options))]
        msg = msg.replace("{s}", s).replace("{rev}", str(rng.randrange(20, 90)))
        t = self._second(self.onset, rng.uniform(-480, 240))
        trail = self._trail("app", t, name, src, msg, rid=rid, service=service)
        cfg = self._config(
            "app",
            self._second(t, rng.uniform(5, 40)),
            f"{rtype} {rid.rsplit('/', 1)[-1]} configuration changed",
            rid=rid,
            rtype=rtype,
            service=service,
        )
        self.out.events += [trail, cfg]
        self.out.red_herrings += [trail.event_id, cfg.event_id]
        return [
            ResourceRecord(resource_id=rid, resource_type=rtype, service=service, role="unrelated")
        ]

    def _alarm(self, series: dict[tuple[str, str], dict[datetime, float]]) -> CanonicalEvent:
        primary = self.faults[0]
        role, metric = primary.sig.alarm
        scale = self.scale_of[(role, metric)]
        spec = next(m for m in self.topo.metrics if (m.role, m.name) == (role, metric))
        peak = next(
            (e.peak for e in primary.sig.effects if (e.role, e.metric) == (role, metric)),
            spec.base * 5 + 1,
        )
        base = spec.base * scale
        threshold = _r(base + 0.25 * (peak * scale - base) * self.severity)
        earliest = self.onset + timedelta(seconds=self.detect_lag)
        values = series[(role, metric)]
        t_alarm, observed = earliest, values[min(values, key=lambda t: abs(t - earliest))]
        for t in self.grid:
            if t >= earliest and values[t] >= threshold:
                t_alarm, observed = t, values[t]
                break
        self.alarm_time = t_alarm
        self.alarm_resource = self.topo.rid(role)
        self.alarm_key = (role, metric)
        name = f"{metric}-{self.topo.short(role)}"
        return CanonicalEvent.build(
            timestamp=t_alarm,
            source=EventSource.ALARM,
            service="cloudwatch",
            resource_id=self.alarm_resource,
            event_type="alarm_state_change",
            metric=metric,
            value=observed,
            severity=Severity.CRITICAL,
            message=f"ALARM: {metric} on {self.alarm_resource} breached threshold {threshold}",
            metadata={
                "alarm_name": name,
                "state": "ALARM",
                "previous_state": "OK",
                "threshold": threshold,
            },
            raw_ref=f"alarm:{name}",
        )

    def _degrade(self, mode: str) -> None:
        rng = self.rng
        keep_roles = {self.alarm_key[0]}
        if mode == "sparse":
            others = sorted({m.role for m in self.topo.metrics} - keep_roles)
            keep_roles.add(rng.choice(others))
        keep_ids = {self.topo.rid(r) for r in keep_roles}
        kept = []
        for ev in self.out.events:
            if ev.source == EventSource.ALARM:
                kept.append(ev)
            elif ev.source == EventSource.CLOUDWATCH_METRIC and ev.resource_id in keep_ids:
                if mode == "alarm_only" or rng.random() >= 0.7:
                    kept.append(ev)
        self.out.events = kept
        self.out.evidence = []
        self.out.red_herrings = []

    # ---------------------------------------------------------------- assembly
    def run(self) -> tuple[ObservableScenario, TruthRecord]:
        spec = self.spec
        self._draw_parameters()
        series = self._metrics()
        self._emit_metrics(series)
        self._background()
        for f in self.faults:
            self._fault_signals(f)
        extra_resources = self._red_herring() if spec.red_herring else []
        self.out.events.append(self._alarm(series))
        if spec.degradation:
            self._degrade(spec.degradation)

        events = sorted(self.out.events, key=lambda e: (e.timestamp, e.event_id))
        ids = [e.event_id for e in events]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"duplicate event ids in {spec.key}")
        evidence = list(dict.fromkeys(self.out.evidence))

        role, metric = self.alarm_key
        incident = IncidentRecord(
            incident_id=self.incident_id,
            title=f"ALARM: {metric} on {self.alarm_resource}",
            description=(
                f"At {self.alarm_time:%Y-%m-%d %H:%M} UTC a CloudWatch alarm on {metric} for "
                f"{self.alarm_resource} entered ALARM state. {SYMPTOMS[(role, metric)]} "
                f"Environment: production web application in {REGION}."
            ),
            alarm_time=self.alarm_time,
            window_start=self.window_start,
            window_end=self.window_end,
            affected_resources=[self.alarm_resource],
            region=REGION,
        )
        observable = ObservableScenario(
            incident=incident,
            resources=list(self.topo.resources.values()) + extra_resources,
            relationships=self.topo.edges,
            events=events,
        )
        truth = TruthRecord(
            incident_id=self.incident_id,
            ground_truth=self._ground_truth(evidence),
            anomaly_labels=self._labels(),
            meta=ScenarioMeta(
                scenario_key=spec.key,
                category=spec.category,
                split="dev",
                topology=spec.topology,
                faults=[
                    InjectedFault(
                        fault=f.sig.fault.value,
                        onset=f.onset,
                        primary_resource_id=self.topo.rid(f.sig.primary_role),
                        trigger_visible=f.visible,
                    )
                    for f in self.faults
                ],
                severity=self.severity,
                noise=self.noise,
                period_seconds=self.period,
                missing_rate=self.missing_rate,
                degradation=spec.degradation,
                red_herring_event_ids=self.out.red_herrings,
            ),
        )
        return observable, truth

    def _ground_truth(self, evidence: list[str]) -> GroundTruth:
        primary = self.faults[0]
        if self.spec.degradation:
            return GroundTruth(
                taxonomy_label=RootCause.INSUFFICIENT_EVIDENCE,
                primary_resource_id=self.alarm_resource,
                root_cause_text="The available telemetry does not contain enough evidence to "
                "determine the root cause; additional logs, CloudTrail and dependency metrics "
                "are required.",
                onset_time=primary.onset,
                evidence_event_ids=[],
                resolution="Collect application logs, CloudTrail and downstream metrics, then "
                "re-diagnose.",
            )
        ctx = self.ctx
        text = " ".join(f.sig.root_cause_text.format_map(ctx) for f in self.faults)
        return GroundTruth(
            taxonomy_label=primary.sig.fault,
            primary_resource_id=self.topo.rid(primary.sig.primary_role),
            root_cause_text=text,
            onset_time=primary.onset,
            evidence_event_ids=evidence,
            resolution=" ".join(f.sig.resolution.format_map(ctx) for f in self.faults),
            secondary_labels=[f.sig.fault for f in self.faults[1:]],
        )

    def _labels(self) -> list[AnomalyLabel]:
        labels = {}
        for f in self.faults:
            for e in f.sig.effects:
                start = f.onset + timedelta(seconds=e.lag_s)
                key = (e.role, e.metric, start)
                labels[key] = AnomalyLabel(
                    resource_id=self.topo.rid(e.role),
                    metric=e.metric,
                    start=start,
                    end=self.window_end,
                )
        return sorted(labels.values(), key=lambda a: (a.start, a.resource_id, a.metric))


def generate_scenario(spec: ScenarioSpec, seed: int) -> tuple[ObservableScenario, TruthRecord]:
    return _Simulator(spec, seed).run()
