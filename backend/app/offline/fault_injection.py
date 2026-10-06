"""Fault injection against the TEST stack in infra/test-stack/template.yaml.

LOCAL-ONLY note: this tool was written but never run against AWS from the build environment.
It defaults to a dry run that prints the plan. Nothing touches AWS without ``--execute``.

    python -m app.offline.fault_injection --stack demo --fault lb_error_spike
    python -m app.offline.fault_injection --stack demo --fault lb_error_spike --execute
    python -m app.offline.fault_injection --stack demo --fault lb_error_spike --revert --execute

Inject one fault at a time, wait for the alarm/symptoms (~10-15 min), capture telemetry with the
real collectors, then revert. Revert state is kept in ``.fault-state/<stack>-<fault>.json``.
Requires the ``aws`` extra (boto3) and, for database load faults, network reach to the DB
(run from inside the VPC, e.g. a bastion or an ECS task).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from app.contracts.taxonomy import FAULT_TYPES

STATE_DIR = Path(".fault-state")


@dataclass
class StackRefs:
    AlbUrl: str
    ListenerArn: str
    ClusterName: str
    ServiceName: str
    TaskRoleName: str
    SecretsPolicyArn: str
    DbInstanceId: str
    DbHost: str
    DbSecretArn: str
    DbSecurityGroupId: str
    AppSecurityGroupId: str
    ConsumerFunctionName: str
    QueueUrl: str

    @classmethod
    def from_outputs(cls, outputs: dict[str, str]) -> StackRefs:
        missing = [f.name for f in fields(cls) if f.name not in outputs]
        if missing:
            raise ValueError(f"stack outputs missing: {missing}")
        return cls(**{f.name: outputs[f.name] for f in fields(cls)})

    @classmethod
    def placeholder(cls) -> StackRefs:
        return cls(**{f.name: f"<{f.name}>" for f in fields(cls)})


class Aws:
    """Lazily created boto3 clients (boto3 is imported only when executing)."""

    def __init__(self, region: str | None):
        import boto3  # noqa: PLC0415 - optional dependency, only needed with --execute

        self._session = boto3.session.Session(region_name=region)
        self._clients: dict[str, Any] = {}

    def __getattr__(self, service: str) -> Any:
        if service.startswith("_"):
            raise AttributeError(service)
        if service not in self._clients:
            # "lambda" is a Python keyword, so the Lambda client is reached as `awslambda`.
            name = "lambda" if service == "awslambda" else service
            self._clients[service] = self._session.client(name)
        return self._clients[service]


Action = Callable[[Aws, StackRefs, dict[str, Any]], None]


@dataclass
class Step:
    description: str
    action: Action


# ----------------------------------------------------------------------------- helpers
def _current_task_def(aws: Aws, r: StackRefs) -> str:
    svc = aws.ecs.describe_services(cluster=r.ClusterName, services=[r.ServiceName])["services"][0]
    return svc["taskDefinition"]


def _register_variant(aws: Aws, base_arn: str, mutate: Callable[[dict], None]) -> str:
    td = aws.ecs.describe_task_definition(taskDefinition=base_arn)["taskDefinition"]
    keep = (
        "family",
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "containerDefinitions",
        "volumes",
        "requiresCompatibilities",
        "cpu",
        "memory",
    )
    spec = {k: td[k] for k in keep if k in td}
    mutate(spec["containerDefinitions"][0])
    return aws.ecs.register_task_definition(**spec)["taskDefinition"]["taskDefinitionArn"]


def _update_service(aws: Aws, r: StackRefs, task_def: str) -> None:
    aws.ecs.update_service(
        cluster=r.ClusterName,
        service=r.ServiceName,
        taskDefinition=task_def,
        forceNewDeployment=True,
    )


def _db_connect(aws: Aws, r: StackRefs):
    import psycopg  # noqa: PLC0415

    secret = json.loads(aws.secretsmanager.get_secret_value(SecretId=r.DbSecretArn)["SecretString"])
    return psycopg.connect(
        host=r.DbHost,
        dbname="postgres",
        user=secret["username"],
        password=secret["password"],
        connect_timeout=5,
    )


def _run_for(seconds: int, workers: int, fn: Callable[[], None]) -> None:
    stop = time.monotonic() + seconds

    def loop() -> None:
        while time.monotonic() < stop:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - load generators keep going on errors
                print(f"  worker error: {exc}")
                time.sleep(1)

    threads = [threading.Thread(target=loop, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ----------------------------------------------------------------------------- fault plans
def _deployment(revert: bool) -> list[Step]:
    if revert:
        return [
            Step(
                "update ECS service back to the saved task definition",
                lambda a, r, s: _update_service(a, r, s["previous_task_def"]),
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        s["previous_task_def"] = _current_task_def(a, r)

        def broken(c: dict) -> None:
            c["command"] = [
                "sh",
                "-c",
                "echo 'Failed to start application: missing required "
                "environment variable DATABASE_URL' >&2; exit 1",
            ]
            c["entryPoint"] = []

        _update_service(a, r, _register_variant(a, s["previous_task_def"], broken))

    return [
        Step("register a task definition whose container exits at startup and deploy it", inject)
    ]


def _connection_exhaustion(revert: bool, hold_s: int = 900, n: int = 120) -> list[Step]:
    if revert:
        return [
            Step(
                "nothing to revert: held connections close when the inject run ends",
                lambda a, r, s: None,
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        conns = []
        for _ in range(n):
            try:
                conns.append(_db_connect(a, r))
            except Exception as exc:  # noqa: BLE001
                print(f"  stopped opening connections: {exc}")
                break
        print(f"  holding {len(conns)} connections for {hold_s}s")
        time.sleep(hold_s)
        for c in conns:
            c.close()

    return [Step(f"open up to {n} PostgreSQL connections and hold them {hold_s}s", inject)]


def _function_timeout(revert: bool) -> list[Step]:
    if revert:
        return [
            Step(
                "restore the consumer function timeout",
                lambda a, r, s: a.awslambda.update_function_configuration(
                    FunctionName=r.ConsumerFunctionName, Timeout=s["previous_timeout"]
                ),
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        cfg = a.awslambda.get_function_configuration(FunctionName=r.ConsumerFunctionName)
        s["previous_timeout"] = cfg["Timeout"]
        a.awslambda.update_function_configuration(FunctionName=r.ConsumerFunctionName, Timeout=3)
        for i in range(0, 500, 10):
            a.sqs.send_message_batch(
                QueueUrl=r.QueueUrl,
                Entries=[
                    {"Id": str(j), "MessageBody": json.dumps({"n": i + j})} for j in range(10)
                ],
            )

    return [
        Step("set consumer Lambda timeout to 3 s and enqueue 500 messages (10 s batches)", inject)
    ]


def _cpu_saturation(revert: bool, seconds: int = 900, workers: int = 64) -> list[Step]:
    if revert:
        return [
            Step(
                "restore ECS desired count",
                lambda a, r, s: a.ecs.update_service(
                    cluster=r.ClusterName, service=r.ServiceName, desiredCount=s["previous_desired"]
                ),
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        svc = a.ecs.describe_services(cluster=r.ClusterName, services=[r.ServiceName])["services"][
            0
        ]
        s["previous_desired"] = svc["desiredCount"]
        a.ecs.update_service(cluster=r.ClusterName, service=r.ServiceName, desiredCount=1)
        _run_for(seconds, workers, lambda: urllib.request.urlopen(r.AlbUrl, timeout=10).read())

    return [
        Step(
            f"scale service to 1 task and drive {workers} concurrent HTTP clients for {seconds}s",
            inject,
        )
    ]


def _lb_error_spike(revert: bool) -> list[Step]:
    if revert:
        return [
            Step(
                "restore listener default actions",
                lambda a, r, s: a.elbv2.modify_listener(
                    ListenerArn=r.ListenerArn, DefaultActions=s["previous_actions"]
                ),
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        listener = a.elbv2.describe_listeners(ListenerArns=[r.ListenerArn])["Listeners"][0]
        s["previous_actions"] = listener["DefaultActions"]
        a.elbv2.modify_listener(
            ListenerArn=r.ListenerArn,
            DefaultActions=[
                {
                    "Type": "fixed-response",
                    "FixedResponseConfig": {
                        "StatusCode": "503",
                        "ContentType": "text/plain",
                        "MessageBody": "Service Unavailable",
                    },
                }
            ],
        )

    return [Step("change listener default action to a fixed 503 response", inject)]


def _iam(revert: bool) -> list[Step]:
    if revert:
        return [
            Step(
                "re-attach the secrets read policy to the task role",
                lambda a, r, s: a.iam.attach_role_policy(
                    RoleName=r.TaskRoleName, PolicyArn=r.SecretsPolicyArn
                ),
            )
        ]
    return [
        Step(
            "detach the secrets read policy from the task role",
            lambda a, r, s: a.iam.detach_role_policy(
                RoleName=r.TaskRoleName, PolicyArn=r.SecretsPolicyArn
            ),
        )
    ]


def _sg_rule(r: StackRefs) -> dict:
    return {
        "GroupId": r.DbSecurityGroupId,
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 5432,
                "ToPort": 5432,
                "UserIdGroupPairs": [{"GroupId": r.AppSecurityGroupId}],
            }
        ],
    }


def _sg(revert: bool) -> list[Step]:
    if revert:
        return [
            Step(
                "re-authorize tcp/5432 from the app SG on the DB SG",
                lambda a, r, s: a.ec2.authorize_security_group_ingress(**_sg_rule(r)),
            )
        ]
    return [
        Step(
            "revoke tcp/5432 from the app SG on the DB SG",
            lambda a, r, s: a.ec2.revoke_security_group_ingress(**_sg_rule(r)),
        )
    ]


def _db_latency(revert: bool, seconds: int = 900, workers: int = 16) -> list[Step]:
    if revert:
        return [
            Step(
                "nothing to revert: query load stops when the inject run ends", lambda a, r, s: None
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        def heavy() -> None:
            with _db_connect(a, r) as conn:
                conn.execute(
                    "SELECT count(*) FROM generate_series(1, 30000000) g "
                    "WHERE md5(g::text) LIKE 'ab%'"
                )

        _run_for(seconds, workers, heavy)

    return [Step(f"run {workers} concurrent heavy scan queries for {seconds}s", inject)]


def _crash_loop(revert: bool) -> list[Step]:
    if revert:
        return [
            Step(
                "update ECS service back to the saved task definition",
                lambda a, r, s: _update_service(a, r, s["previous_task_def"]),
            )
        ]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        s["previous_task_def"] = _current_task_def(a, r)

        def oom(c: dict) -> None:
            c["memory"] = 128
            c["entryPoint"] = []
            c["command"] = ["sh", "-c", "sleep 20; head -c 400m /dev/zero | tail"]

        _update_service(a, r, _register_variant(a, s["previous_task_def"], oom))

    return [
        Step("deploy a task definition whose container exceeds a 128 MiB hard memory limit", inject)
    ]


def _queue_backlog(revert: bool, messages: int = 5000) -> list[Step]:
    if revert:

        def restore(a: Aws, r: StackRefs, s: dict) -> None:
            if s.get("previous_concurrency") is None:
                a.awslambda.delete_function_concurrency(FunctionName=r.ConsumerFunctionName)
            else:
                a.awslambda.put_function_concurrency(
                    FunctionName=r.ConsumerFunctionName,
                    ReservedConcurrentExecutions=s["previous_concurrency"],
                )

        return [Step("restore consumer reserved concurrency", restore)]

    def inject(a: Aws, r: StackRefs, s: dict) -> None:
        cur = a.awslambda.get_function_concurrency(FunctionName=r.ConsumerFunctionName)
        s["previous_concurrency"] = cur.get("ReservedConcurrentExecutions")
        a.awslambda.put_function_concurrency(
            FunctionName=r.ConsumerFunctionName, ReservedConcurrentExecutions=1
        )
        for i in range(0, messages, 10):
            a.sqs.send_message_batch(
                QueueUrl=r.QueueUrl,
                Entries=[
                    {"Id": str(j), "MessageBody": json.dumps({"n": i + j})} for j in range(10)
                ],
            )

    return [Step(f"set consumer reserved concurrency to 1 and enqueue {messages} messages", inject)]


PLANS: dict[str, Callable[[bool], list[Step]]] = {
    "deployment_failure": _deployment,
    "connection_exhaustion": _connection_exhaustion,
    "function_timeout": _function_timeout,
    "cpu_saturation": _cpu_saturation,
    "lb_error_spike": _lb_error_spike,
    "iam_permission_failure": _iam,
    "security_group_misconfiguration": _sg,
    "db_latency_increase": _db_latency,
    "task_crash_loop": _crash_loop,
    "queue_backlog": _queue_backlog,
}
assert set(PLANS) == {f.value for f in FAULT_TYPES}


def plan(fault: str, revert: bool = False) -> list[Step]:
    if fault not in PLANS:
        raise ValueError(f"unknown fault {fault!r}; choose from {sorted(PLANS)}")
    return PLANS[fault](revert)


def _stack_outputs(aws: Aws, stack: str) -> dict[str, str]:
    desc = aws.cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inject or revert one fault on the TEST stack.")
    p.add_argument("--stack", required=True, help="CloudFormation stack name")
    p.add_argument("--fault", required=True, choices=sorted(PLANS))
    p.add_argument("--revert", action="store_true")
    p.add_argument("--execute", action="store_true", help="actually call AWS (default: dry run)")
    p.add_argument("--region", default=None)
    args = p.parse_args(argv)

    steps = plan(args.fault, args.revert)
    verb = "REVERT" if args.revert else "INJECT"
    print(
        f"{verb} {args.fault} on stack {args.stack} "
        f"({'EXECUTE' if args.execute else 'dry run, nothing sent to AWS'})"
    )
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step.description}")
    if not args.execute:
        return 0

    aws = Aws(args.region)
    refs = StackRefs.from_outputs(_stack_outputs(aws, args.stack))
    state_path = STATE_DIR / f"{args.stack}-{args.fault}.json"
    state: dict[str, Any] = json.loads(state_path.read_text()) if state_path.exists() else {}
    if (
        args.revert
        and not state
        and args.fault
        in (
            "deployment_failure",
            "function_timeout",
            "cpu_saturation",
            "lb_error_spike",
            "task_crash_loop",
            "queue_backlog",
        )
    ):
        raise SystemExit(f"no saved state at {state_path}; cannot revert safely")
    for step in steps:
        print(f"-> {step.description}")
        step.action(aws, refs, state)
        STATE_DIR.mkdir(exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, default=str))
    print(f"started at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}; state in {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
