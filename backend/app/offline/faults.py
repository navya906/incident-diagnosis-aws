"""Fault signatures: how each root cause shows up in metrics, logs, CloudTrail and AWS Config.

Peaks are absolute values at severity 1.0 (per-minute values for count metrics); the simulator
scales the delta from baseline by the scenario severity. ``key=True`` marks signals whose first
occurrences become ground-truth evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts.taxonomy import RootCause as RC
from app.offline.topology import Topology


@dataclass(frozen=True)
class Effect:
    role: str
    metric: str
    peak: float
    lag_s: int = 0
    ramp_s: int = 0
    shape: str = "ramp"  # ramp | oscillate
    key: bool = False


@dataclass(frozen=True)
class FaultLog:
    role: str
    level: str  # INFO | WARNING | ERROR | CRITICAL
    template: str
    lag_s: int
    count: int
    spread_s: int
    key: bool = False


@dataclass(frozen=True)
class FaultTrail:
    role: str
    event_name: str
    event_source: str
    message: str
    offset_s: int
    optional: bool = True  # emitted only when the trigger is visible
    error_code: str | None = None
    key: bool = True


@dataclass(frozen=True)
class FaultConfig:
    role: str
    message: str
    offset_s: int
    optional: bool = True
    key: bool = True


@dataclass(frozen=True)
class FaultSignature:
    fault: RC
    primary_role: str
    alarm: tuple[str, str]  # (role, metric)
    root_cause_text: str
    resolution: str
    effects: list[Effect]
    logs: list[FaultLog] = field(default_factory=list)
    trail: list[FaultTrail] = field(default_factory=list)
    config: list[FaultConfig] = field(default_factory=list)
    trigger_visible_prob: float = 1.0


COMPATIBLE_TOPOLOGIES: dict[RC, tuple[str, ...]] = {
    RC.DEPLOYMENT_FAILURE: ("ecs", "lambda", "ec2"),
    RC.CONNECTION_EXHAUSTION: ("ecs", "lambda", "ec2"),
    RC.FUNCTION_TIMEOUT: ("lambda",),
    RC.CPU_SATURATION: ("ecs", "ec2"),
    RC.LB_ERROR_SPIKE: ("ecs", "lambda", "ec2"),
    RC.IAM_PERMISSION_FAILURE: ("ecs", "lambda", "sqs"),
    RC.SECURITY_GROUP_MISCONFIGURATION: ("ecs", "ec2", "lambda"),
    RC.DB_LATENCY_INCREASE: ("ecs", "lambda", "ec2"),
    RC.TASK_CRASH_LOOP: ("ecs",),
    RC.QUEUE_BACKLOG: ("sqs",),
}

_T5XX = ("alb", "HTTPCode_Target_5XX_Count")
_TRT = ("alb", "TargetResponseTime")


def signature(fault: RC, topo: Topology) -> FaultSignature:
    if topo.kind not in COMPATIBLE_TOPOLOGIES.get(fault, ()):
        raise ValueError(f"{fault} is not defined for topology {topo.kind}")
    return _BUILDERS[fault](topo)


def _deployment(t: Topology) -> FaultSignature:
    k = t.kind
    effects = [
        Effect("alb", "HTTPCode_Target_5XX_Count", 500, lag_s=30, ramp_s=120, key=True),
        Effect("alb", "HTTPCode_ELB_5XX_Count", 40, lag_s=90, ramp_s=60),
        Effect("alb", "TargetResponseTime", 0.6, lag_s=60, ramp_s=120),
    ]
    if k == "ecs":
        effects += [
            Effect("app", "RunningTaskCount", 1, ramp_s=60, key=True),
            Effect("alb", "UnHealthyHostCount", 3, lag_s=30, ramp_s=60),
        ]
        trail = [
            FaultTrail(
                "app",
                "RegisterTaskDefinition",
                "ecs.amazonaws.com",
                "RegisterTaskDefinition: web:{rev} registered by deployer",
                -90,
            ),
            FaultTrail(
                "app",
                "UpdateService",
                "ecs.amazonaws.com",
                "UpdateService: {app} now uses task definition web:{rev}",
                -30,
            ),
        ]
    elif k == "lambda":
        effects.append(Effect("app", "Errors", 700, ramp_s=60, key=True))
        trail = [
            FaultTrail(
                "app",
                "UpdateFunctionCode20150331v2",
                "lambda.amazonaws.com",
                "UpdateFunctionCode: new code package deployed to {app}",
                -30,
            )
        ]
    else:
        effects.append(Effect("alb", "UnHealthyHostCount", 2, lag_s=60, ramp_s=60, key=True))
        trail = [
            FaultTrail(
                "app",
                "CreateDeployment",
                "codedeploy.amazonaws.com",
                "CreateDeployment: revision {rev} deployed to {app}",
                -30,
            )
        ]
    return FaultSignature(
        fault=RC.DEPLOYMENT_FAILURE,
        primary_role="app",
        alarm=_T5XX,
        root_cause_text="A new release of {app} was deployed with a broken configuration; "
        "the new version fails at startup and requests to it return errors.",
        resolution="Roll back {app} to the previous revision; add pre-deploy config validation.",
        effects=effects,
        logs=[
            FaultLog(
                t.actor,
                "ERROR",
                "Failed to start application: missing required "
                "environment variable DATABASE_URL (release {rev})",
                5,
                8,
                240,
                key=True,
            ),
            FaultLog("app", "WARNING", "Health check GET /healthz returned 503", 30, 6, 300),
        ],
        trail=trail,
        config=[FaultConfig("app", "Configuration of {app} changed: revision {rev}", -20)],
        trigger_visible_prob=0.9,
    )


def _connection(t: Topology) -> FaultSignature:
    effects = [
        Effect("db", "DatabaseConnections", 100, ramp_s=240, key=True),
        Effect("db", "CPUUtilization", 60, lag_s=120, ramp_s=180),
        Effect("alb", "RequestCount", 1500, ramp_s=300),
        Effect("alb", "HTTPCode_Target_5XX_Count", 300, lag_s=240, ramp_s=120),
        Effect("alb", "TargetResponseTime", 1.5, lag_s=180, ramp_s=180),
    ]
    if t.kind == "lambda":
        effects += [
            Effect("app", "ConcurrentExecutions", 200, ramp_s=180),
            Effect("app", "Duration", 1200, lag_s=200, ramp_s=120),
        ]
    return FaultSignature(
        fault=RC.CONNECTION_EXHAUSTION,
        primary_role="db",
        alarm=_T5XX,
        root_cause_text="{db} reached its max_connections limit; new connections from {app} "
        "were refused, causing request failures.",
        resolution="Add connection pooling (RDS Proxy / pool limits) on {app}; "
        "tune max_connections.",
        effects=effects,
        logs=[
            FaultLog(
                "db",
                "ERROR",
                "FATAL: remaining connection slots are reserved for "
                "non-replication superuser connections",
                240,
                10,
                600,
                key=True,
            ),
            FaultLog(
                t.actor,
                "ERROR",
                "OperationalError: connection to server at {db} failed: "
                "FATAL: sorry, too many clients already",
                270,
                10,
                600,
                key=True,
            ),
        ],
        trail=[
            FaultTrail(
                "app",
                "UpdateService",
                "ecs.amazonaws.com",
                "UpdateService: desiredCount of {app} changed from 4 to 12",
                -300,
                key=False,
            )
        ]
        if t.kind == "ecs"
        else [],
        trigger_visible_prob=0.5,
    )


def _function_timeout(t: Topology) -> FaultSignature:
    return FaultSignature(
        fault=RC.FUNCTION_TIMEOUT,
        primary_role="app",
        alarm=("app", "Errors"),
        root_cause_text="{app} invocations exceed the configured function timeout (3 s) "
        "and are terminated before completing.",
        resolution="Restore the function timeout on {app} to 30 s and alarm on Duration "
        "vs timeout.",
        effects=[
            Effect("app", "Duration", 3000, ramp_s=120, key=True),
            Effect("app", "Errors", 600, lag_s=60, ramp_s=120, key=True),
            Effect("app", "Throttles", 30, lag_s=200, ramp_s=120),
            Effect("app", "ConcurrentExecutions", 150, lag_s=120, ramp_s=120),
            Effect("alb", "HTTPCode_Target_5XX_Count", 250, lag_s=120, ramp_s=120),
            Effect("alb", "TargetResponseTime", 3.0, lag_s=90, ramp_s=120),
        ],
        logs=[
            FaultLog(
                "app", "ERROR", "{sess} Task timed out after 3.00 seconds", 30, 12, 900, key=True
            ),
            FaultLog(
                "app", "WARNING", "{sess} Duration {ms} ms approaching configured limit", 10, 5, 300
            ),
        ],
        trail=[
            FaultTrail(
                "app",
                "UpdateFunctionConfiguration20150331v2",
                "lambda.amazonaws.com",
                "UpdateFunctionConfiguration: Timeout of {app} changed from 30 to 3",
                -120,
            )
        ],
        config=[FaultConfig("app", "AWS::Lambda::Function {app}: Timeout 30 -> 3", -110)],
        trigger_visible_prob=0.7,
    )


def _cpu(t: Topology) -> FaultSignature:
    return FaultSignature(
        fault=RC.CPU_SATURATION,
        primary_role="app",
        alarm=_TRT,
        root_cause_text="{app} CPU is saturated by a traffic increase; requests queue up and "
        "response times rise.",
        resolution="Scale out {app} (more tasks/instances) and set CPU-based autoscaling.",
        effects=[
            Effect("app", "CPUUtilization", 99, ramp_s=180, key=True),
            Effect("alb", "RequestCount", 2000, ramp_s=240),
            Effect("alb", "TargetResponseTime", 2.0, lag_s=120, ramp_s=180),
            Effect("alb", "HTTPCode_Target_5XX_Count", 120, lag_s=240, ramp_s=180),
        ],
        logs=[
            FaultLog("app", "WARNING", "Request queue depth {n} exceeds threshold 50", 150, 8, 900),
            FaultLog(
                "app",
                "ERROR",
                "Worker pool saturated, rejecting request (busy={n})",
                240,
                6,
                900,
                key=True,
            ),
        ],
    )


def _lb_error(t: Topology) -> FaultSignature:
    return FaultSignature(
        fault=RC.LB_ERROR_SPIKE,
        primary_role="alb",
        alarm=("alb", "HTTPCode_ELB_5XX_Count"),
        root_cause_text="{alb} itself is returning 5xx responses after a listener change; "
        "requests never reach healthy targets.",
        resolution="Revert the listener rule on {alb}; add change review for listener edits.",
        effects=[
            Effect("alb", "HTTPCode_ELB_5XX_Count", 600, ramp_s=60, key=True),
            Effect("alb", "RequestCount", 700, lag_s=60, ramp_s=120),
        ],
        logs=[
            FaultLog(
                "alb",
                "ERROR",
                "type=http elb_status_code=503 target_status_code=- "
                "request_processing_time=-1 target=-",
                0,
                12,
                900,
                key=True,
            )
        ],
        trail=[
            FaultTrail(
                "alb",
                "ModifyListener",
                "elasticloadbalancing.amazonaws.com",
                "ModifyListener: default action of {alb} listener :443 changed",
                -60,
            )
        ],
        config=[FaultConfig("alb", "AWS::ElasticLoadBalancingV2::Listener on {alb} changed", -50)],
        trigger_visible_prob=0.6,
    )


def _iam(t: Topology) -> FaultSignature:
    a = t.actor
    if t.kind == "sqs":
        effects = [
            Effect("consumer", "Errors", 500, lag_s=30, ramp_s=60, key=True),
            Effect("queue", "ApproximateNumberOfMessagesVisible", 3000, lag_s=180, ramp_s=600),
            Effect("queue", "ApproximateAgeOfOldestMessage", 600, lag_s=200, ramp_s=600),
            Effect("queue", "NumberOfMessagesDeleted", 50, lag_s=30, ramp_s=60),
        ]
        alarm = ("queue", "ApproximateAgeOfOldestMessage")
    else:
        effects = [
            Effect(
                "alb", "HTTPCode_Target_5XX_Count", 350, lag_s=60, ramp_s=60, key=t.kind == "ecs"
            ),
            Effect("alb", "TargetResponseTime", 0.3, lag_s=60, ramp_s=60),
        ]
        if t.kind == "lambda":
            effects.append(Effect("app", "Errors", 650, lag_s=30, ramp_s=60, key=True))
        alarm = _T5XX
    return FaultSignature(
        fault=RC.IAM_PERMISSION_FAILURE,
        primary_role="role",
        alarm=alarm,
        root_cause_text="A policy was detached from {role}; {actor} can no longer read its "
        "secret and every request that needs it fails with AccessDenied.",
        resolution="Re-attach the secrets read policy to {role}; manage IAM through IaC only.",
        effects=effects,
        logs=[
            FaultLog(
                a,
                "ERROR",
                "AccessDeniedException: User: arn:aws:sts::123456789012:"
                "assumed-role/{role_name}/{sess} is not authorized to perform: "
                "secretsmanager:GetSecretValue",
                20,
                10,
                900,
                key=True,
            )
        ],
        trail=[
            FaultTrail(
                "role",
                "DetachRolePolicy",
                "iam.amazonaws.com",
                "DetachRolePolicy: SecretsReadAccess detached from {role_name}",
                -45,
            ),
            FaultTrail(
                "role",
                "GetSecretValue",
                "secretsmanager.amazonaws.com",
                "GetSecretValue denied for {role_name}",
                30,
                optional=False,
                error_code="AccessDenied",
            ),
        ],
        config=[FaultConfig("role", "AWS::IAM::Role {role_name}: attached policies changed", -40)],
        trigger_visible_prob=0.85,
    )


def _sg(t: Topology) -> FaultSignature:
    effects = [
        Effect("db", "DatabaseConnections", 1, ramp_s=30, key=True),
        Effect("db", "CPUUtilization", 3, lag_s=30, ramp_s=60),
        Effect("alb", "TargetResponseTime", 5.0, lag_s=60, ramp_s=60),
        Effect("alb", "HTTPCode_Target_5XX_Count", 450, lag_s=90, ramp_s=90),
        Effect("alb", "HTTPCode_ELB_5XX_Count", 30, lag_s=120, ramp_s=60),
    ]
    if t.kind == "lambda":
        effects += [
            Effect("app", "Duration", 5000, lag_s=30, ramp_s=60),
            Effect("app", "Errors", 500, lag_s=60, ramp_s=60),
        ]
    return FaultSignature(
        fault=RC.SECURITY_GROUP_MISCONFIGURATION,
        primary_role="sg",
        alarm=_TRT,
        root_cause_text="The ingress rule allowing {app} to reach {db} on port 5432 was removed "
        "from {sg}; database connections time out.",
        resolution="Restore the tcp/5432 ingress rule on {sg}; enforce SG changes through IaC.",
        effects=effects,
        logs=[
            FaultLog(
                t.actor,
                "ERROR",
                "could not connect to server: Connection timed out. "
                'Is the server running on host "{db_name}" and accepting TCP/IP '
                "connections on port 5432?",
                60,
                12,
                900,
                key=True,
            )
        ],
        trail=[
            FaultTrail(
                "sg",
                "RevokeSecurityGroupIngress",
                "ec2.amazonaws.com",
                "RevokeSecurityGroupIngress: tcp/5432 removed from {sg_name}",
                -20,
            )
        ],
        config=[FaultConfig("sg", "AWS::EC2::SecurityGroup {sg_name}: ingress rules changed", -15)],
        trigger_visible_prob=0.9,
    )


def _db_latency(t: Topology) -> FaultSignature:
    effects = [
        Effect("db", "ReadLatency", 0.05, ramp_s=180, key=True),
        Effect("db", "WriteLatency", 0.08, ramp_s=180, key=True),
        Effect("db", "DiskQueueDepth", 25, ramp_s=150),
        Effect("db", "ReadIOPS", 2500, ramp_s=150),
        Effect("db", "CPUUtilization", 55, lag_s=60, ramp_s=180),
        Effect("alb", "TargetResponseTime", 1.4, lag_s=120, ramp_s=180),
        Effect("alb", "HTTPCode_Target_5XX_Count", 90, lag_s=300, ramp_s=180),
    ]
    if t.kind == "lambda":
        effects.append(Effect("app", "Duration", 1800, lag_s=120, ramp_s=180))
    return FaultSignature(
        fault=RC.DB_LATENCY_INCREASE,
        primary_role="db",
        alarm=_TRT,
        root_cause_text="{db} read/write latency increased after its storage throughput was "
        "reduced; queries from {app} slow down.",
        resolution="Restore provisioned storage throughput on {db}; alarm on Read/WriteLatency.",
        effects=effects,
        logs=[
            FaultLog(
                "db",
                "WARNING",
                "duration: {ms} ms  statement: SELECT * FROM orders WHERE customer_id = $1",
                60,
                14,
                900,
                key=True,
            ),
            FaultLog(t.actor, "WARNING", "Slow query detected: {ms} ms", 120, 8, 900),
        ],
        trail=[
            FaultTrail(
                "db",
                "ModifyDBInstance",
                "rds.amazonaws.com",
                "ModifyDBInstance: storage throughput of {db_name} reduced",
                -300,
            )
        ],
        config=[
            FaultConfig("db", "AWS::RDS::DBInstance {db_name}: StorageThroughput changed", -290)
        ],
        trigger_visible_prob=0.4,
    )


def _crash_loop(t: Topology) -> FaultSignature:
    return FaultSignature(
        fault=RC.TASK_CRASH_LOOP,
        primary_role="app",
        alarm=("alb", "UnHealthyHostCount"),
        root_cause_text="Tasks of {app} are repeatedly killed for exceeding their memory limit "
        "and restarted, so healthy capacity keeps dropping.",
        resolution="Raise the task memory limit of {app} and fix the memory growth.",
        effects=[
            Effect("app", "RunningTaskCount", 1.5, shape="oscillate", key=True),
            Effect("app", "MemoryUtilization", 99, shape="oscillate", key=True),
            Effect("alb", "UnHealthyHostCount", 2, lag_s=60, ramp_s=60),
            Effect("alb", "HTTPCode_Target_5XX_Count", 250, lag_s=90, ramp_s=90),
            Effect("alb", "TargetResponseTime", 0.5, lag_s=90, ramp_s=90),
        ],
        logs=[
            FaultLog(
                "app",
                "ERROR",
                "Task stopped: OutOfMemoryError: Container killed due to "
                "memory usage (exit code 137)",
                20,
                10,
                1200,
                key=True,
            ),
            FaultLog("app", "INFO", "Starting new task for service {app_name}", 40, 8, 1200),
        ],
        trail=[
            FaultTrail(
                "app",
                "RegisterTaskDefinition",
                "ecs.amazonaws.com",
                "RegisterTaskDefinition: web:{rev} memory 1024 -> 256",
                -180,
            )
        ],
        config=[FaultConfig("app", "AWS::ECS::TaskDefinition web:{rev} registered", -175)],
        trigger_visible_prob=0.5,
    )


def _queue_backlog(t: Topology) -> FaultSignature:
    return FaultSignature(
        fault=RC.QUEUE_BACKLOG,
        primary_role="consumer",
        alarm=("queue", "ApproximateAgeOfOldestMessage"),
        root_cause_text="{consumer} reserved concurrency was lowered, so it is throttled and "
        "cannot drain {queue}; messages accumulate.",
        resolution="Restore reserved concurrency on {consumer}; alarm on queue age.",
        effects=[
            Effect("queue", "ApproximateNumberOfMessagesVisible", 25000, ramp_s=600, key=True),
            Effect("queue", "ApproximateAgeOfOldestMessage", 3600, lag_s=60, ramp_s=600, key=True),
            Effect("queue", "NumberOfMessagesDeleted", 60, ramp_s=60),
            Effect("consumer", "Throttles", 400, lag_s=30, ramp_s=60, key=True),
            Effect("consumer", "ConcurrentExecutions", 5, ramp_s=60),
            Effect("consumer", "Invocations", 80, ramp_s=60),
        ],
        logs=[
            FaultLog(
                "consumer",
                "WARNING",
                "TooManyRequestsException: Rate Exceeded (reserved concurrency reached)",
                30,
                10,
                900,
                key=True,
            )
        ],
        trail=[
            FaultTrail(
                "consumer",
                "PutFunctionConcurrency20171031",
                "lambda.amazonaws.com",
                "PutFunctionConcurrency: ReservedConcurrentExecutions of {consumer_name} set to 5",
                -120,
            )
        ],
        config=[
            FaultConfig(
                "consumer",
                "AWS::Lambda::Function {consumer_name}: reserved concurrency 50 -> 5",
                -110,
            )
        ],
        trigger_visible_prob=0.65,
    )


_BUILDERS = {
    RC.DEPLOYMENT_FAILURE: _deployment,
    RC.CONNECTION_EXHAUSTION: _connection,
    RC.FUNCTION_TIMEOUT: _function_timeout,
    RC.CPU_SATURATION: _cpu,
    RC.LB_ERROR_SPIKE: _lb_error,
    RC.IAM_PERMISSION_FAILURE: _iam,
    RC.SECURITY_GROUP_MISCONFIGURATION: _sg,
    RC.DB_LATENCY_INCREASE: _db_latency,
    RC.TASK_CRASH_LOOP: _crash_loop,
    RC.QUEUE_BACKLOG: _queue_backlog,
}

#: Blinded symptom text, keyed by the alarm that fired. Never names a cause.
SYMPTOMS: dict[tuple[str, str], str] = {
    ("alb", "HTTPCode_Target_5XX_Count"): "Users are receiving HTTP 5xx errors from the "
    "application.",
    ("alb", "TargetResponseTime"): "Users report that pages are slow to load or hang.",
    ("alb", "HTTPCode_ELB_5XX_Count"): "Some clients are receiving 503 responses.",
    ("alb", "UnHealthyHostCount"): "Fewer healthy targets than expected are registered behind "
    "the load balancer and some requests fail.",
    ("app", "Errors"): "Invocations of the API function are failing.",
    ("queue", "ApproximateAgeOfOldestMessage"): "Asynchronous jobs are completing much later "
    "than usual.",
}
