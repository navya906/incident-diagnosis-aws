"""Reference topologies (Internet -> ALB -> ECS -> RDS, plus Lambda, EC2 and SQS variants)."""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.offline.models import RelationshipRecord, ResourceRecord

ACCOUNT_ID = "123456789012"
REGION = "us-east-1"
TOPOLOGY_KINDS = ("ecs", "lambda", "ec2", "sqs")


@dataclass(frozen=True)
class MetricSpec:
    role: str
    namespace: str
    name: str
    base: float  # per-minute value for count metrics
    sd: float
    lo: float = 0.0
    hi: float | None = None
    count: bool = False  # CloudWatch Sum: scales with the metric period
    integer: bool = False
    unit: str = ""

    @property
    def stat(self) -> str:
        return "Sum" if self.count else "Average"


def _alb() -> list[MetricSpec]:
    ns = "AWS/ApplicationELB"
    return [
        MetricSpec("alb", ns, "RequestCount", 1200, 60, count=True, unit="Count"),
        MetricSpec("alb", ns, "HTTPCode_Target_5XX_Count", 2, 1.5, count=True, unit="Count"),
        MetricSpec("alb", ns, "HTTPCode_ELB_5XX_Count", 0.5, 0.5, count=True, unit="Count"),
        MetricSpec("alb", ns, "TargetResponseTime", 0.12, 0.015, lo=0.001, unit="Seconds"),
        MetricSpec("alb", ns, "UnHealthyHostCount", 0, 0, integer=True, unit="Count"),
    ]


def _rds() -> list[MetricSpec]:
    ns = "AWS/RDS"
    return [
        MetricSpec("db", ns, "CPUUtilization", 25, 3, hi=100, unit="Percent"),
        MetricSpec("db", ns, "DatabaseConnections", 60, 5, hi=100, integer=True, unit="Count"),
        MetricSpec("db", ns, "ReadLatency", 0.002, 0.0003, lo=0.0001, unit="Seconds"),
        MetricSpec("db", ns, "WriteLatency", 0.004, 0.0005, lo=0.0001, unit="Seconds"),
        MetricSpec("db", ns, "DiskQueueDepth", 0.5, 0.2, unit="Count"),
        MetricSpec("db", ns, "ReadIOPS", 300, 30, unit="Count/Second"),
    ]


def _ecs(role: str) -> list[MetricSpec]:
    ns = "AWS/ECS"
    return [
        MetricSpec(role, ns, "CPUUtilization", 35, 4, hi=100, unit="Percent"),
        MetricSpec(role, ns, "MemoryUtilization", 55, 3, hi=100, unit="Percent"),
        MetricSpec(role, "ECS/ContainerInsights", "RunningTaskCount", 4, 0, integer=True),
    ]


def _lambda(role: str, invocations: float) -> list[MetricSpec]:
    ns = "AWS/Lambda"
    return [
        MetricSpec(role, ns, "Invocations", invocations, invocations * 0.05, count=True),
        MetricSpec(role, ns, "Errors", 1, 1, count=True, integer=True),
        MetricSpec(role, ns, "Duration", 220, 25, lo=1, unit="Milliseconds"),
        MetricSpec(role, ns, "Throttles", 0, 0.2, count=True, integer=True),
        MetricSpec(role, ns, "ConcurrentExecutions", 40, 4, integer=True),
    ]


def _ec2(role: str) -> list[MetricSpec]:
    ns = "AWS/EC2"
    return [
        MetricSpec(role, ns, "CPUUtilization", 30, 4, hi=100, unit="Percent"),
        MetricSpec(role, ns, "StatusCheckFailed", 0, 0, integer=True),
    ]


def _sqs() -> list[MetricSpec]:
    ns = "AWS/SQS"
    return [
        MetricSpec("queue", ns, "ApproximateNumberOfMessagesVisible", 20, 8, integer=True),
        MetricSpec("queue", ns, "ApproximateAgeOfOldestMessage", 5, 2, unit="Seconds"),
        MetricSpec("queue", ns, "NumberOfMessagesSent", 600, 40, count=True, integer=True),
        MetricSpec("queue", ns, "NumberOfMessagesDeleted", 600, 40, count=True, integer=True),
    ]


@dataclass
class Topology:
    kind: str
    resources: dict[str, ResourceRecord]
    edges: list[RelationshipRecord]
    metrics: list[MetricSpec]
    log_groups: dict[str, str]

    def rid(self, role: str) -> str:
        return self.resources[role].resource_id

    def service(self, role: str) -> str:
        return self.resources[role].service

    def short(self, role: str) -> str:
        return self.rid(role).rsplit("/", 1)[-1]

    @property
    def actor(self) -> str:
        """The compute component that does the work (consumer in the async variant)."""
        return "consumer" if self.kind == "sqs" else "app"

    def has(self, role: str) -> bool:
        return role in self.resources


def _res(role: str, rid: str, rtype: str, service: str, **attrs) -> ResourceRecord:
    return ResourceRecord(
        resource_id=rid, resource_type=rtype, service=service, role=role, attributes=attrs
    )


def build_topology(kind: str, rng: random.Random) -> Topology:
    if kind not in TOPOLOGY_KINDS:
        raise ValueError(f"unknown topology {kind!r}")
    s = f"{rng.getrandbits(16):04x}"
    res: dict[str, ResourceRecord] = {
        "alb": _res(
            "alb",
            f"alb/app-lb-{s}",
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "elasticloadbalancing",
            scheme="internet-facing",
        ),
        "db": _res(
            "db",
            f"rds/db-{s}",
            "AWS::RDS::DBInstance",
            "rds",
            engine="postgres",
            max_connections=100,
        ),
        "sg": _res(
            "sg",
            f"sg/sg-{rng.getrandbits(32):08x}",
            "AWS::EC2::SecurityGroup",
            "ec2",
            purpose="database ingress",
        ),
    }
    metrics = _alb() + _rds()
    logs: dict[str, str] = {
        "db": f"/aws/rds/instance/db-{s}/postgresql",
        "alb": f"/aws/elb/app-lb-{s}/access",
    }

    if kind in ("ecs", "sqs"):
        res["app"] = _res(
            "app",
            f"ecs/service/web-{s}",
            "AWS::ECS::Service",
            "ecs",
            launch_type="FARGATE",
            desired_count=4,
        )
        metrics += _ecs("app")
        logs["app"] = f"/ecs/web-{s}"
    elif kind == "lambda":
        res["app"] = _res(
            "app", f"lambda/function/api-{s}", "AWS::Lambda::Function", "lambda", timeout_seconds=30
        )
        metrics += _lambda("app", 900)
        logs["app"] = f"/aws/lambda/api-{s}"
    else:
        res["app"] = _res(
            "app",
            f"ec2/instance/i-{rng.getrandbits(64):016x}",
            "AWS::EC2::Instance",
            "ec2",
            instance_type="t3.medium",
        )
        metrics += _ec2("app")
        logs["app"] = f"/app/web-{s}"

    if kind == "sqs":
        res["queue"] = _res("queue", f"sqs/queue/jobs-{s}", "AWS::SQS::Queue", "sqs")
        res["consumer"] = _res(
            "consumer",
            f"lambda/function/worker-{s}",
            "AWS::Lambda::Function",
            "lambda",
            timeout_seconds=60,
            reserved_concurrency=50,
        )
        metrics += _sqs() + _lambda("consumer", 600)
        logs["consumer"] = f"/aws/lambda/worker-{s}"

    actor = "consumer" if kind == "sqs" else "app"
    res["role"] = _res(
        "role",
        f"iam/role/{res[actor].resource_id.rsplit('/', 1)[-1]}-exec",
        "AWS::IAM::Role",
        "iam",
    )

    def edge(a: str, b: str, t: str) -> RelationshipRecord:
        return RelationshipRecord(
            source_id=res[a].resource_id, target_id=res[b].resource_id, relation_type=t
        )

    edges = [
        edge("alb", "app", "routes_to"),
        edge("db", "sg", "secured_by"),
        edge(actor, "role", "assumes_role"),
    ]
    if kind == "sqs":
        edges += [
            edge("app", "queue", "sends_to"),
            edge("consumer", "queue", "polls"),
            edge("consumer", "db", "connects_to"),
        ]
    else:
        edges.append(edge("app", "db", "connects_to"))
    return Topology(kind=kind, resources=res, edges=edges, metrics=metrics, log_groups=logs)
