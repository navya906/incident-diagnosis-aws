"""Builds a small mocked AWS environment (moto) mirroring the reference topology.

ALB -> ECS service -> RDS (via SG rule), Lambda consumer <- SQS, IAM roles, log groups, metrics.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta

import boto3

REGION = "us-east-1"
_ASSUME = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": ["ecs-tasks.amazonaws.com", "lambda.amazonaws.com"]},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


@dataclass
class Env:
    alb_arn: str
    tg_arn: str
    service_arn: str
    db_arn: str
    db_sg: str
    app_sg: str
    function_arn: str
    queue_arn: str
    task_role_arn: str
    log_group: str
    t0: datetime


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.py", "def handler(e, c):\n    return 1\n")
    return buf.getvalue()


def build(t0: datetime) -> Env:
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"{REGION}{z}")[
            "Subnet"
        ]["SubnetId"]
        for i, z in ((1, "a"), (2, "b"))
    ]
    alb_sg = ec2.create_security_group(GroupName="alb", Description="alb", VpcId=vpc)["GroupId"]
    app_sg = ec2.create_security_group(GroupName="app", Description="app", VpcId=vpc)["GroupId"]
    db_sg = ec2.create_security_group(GroupName="db", Description="db", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=db_sg,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 5432,
                "ToPort": 5432,
                "UserIdGroupPairs": [{"GroupId": app_sg}],
            }
        ],
    )

    iam = boto3.client("iam", region_name=REGION)
    task_role = iam.create_role(RoleName="web-task", AssumeRolePolicyDocument=_ASSUME)["Role"][
        "Arn"
    ]
    fn_role = iam.create_role(RoleName="worker-exec", AssumeRolePolicyDocument=_ASSUME)["Role"][
        "Arn"
    ]

    elb = boto3.client("elbv2", region_name=REGION)
    alb = elb.create_load_balancer(Name="app-lb", Subnets=subnets, SecurityGroups=[alb_sg])[
        "LoadBalancers"
    ][0]["LoadBalancerArn"]
    tg = elb.create_target_group(
        Name="web-tg", Protocol="HTTP", Port=80, VpcId=vpc, TargetType="ip"
    )["TargetGroups"][0]["TargetGroupArn"]
    elb.create_listener(
        LoadBalancerArn=alb,
        Protocol="HTTP",
        Port=80,
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg}],
    )

    logs = boto3.client("logs", region_name=REGION)
    group = "/ecs/web"
    logs.create_log_group(logGroupName=group)
    logs.create_log_stream(logGroupName=group, logStreamName="web/web/abc")

    ecs = boto3.client("ecs", region_name=REGION)
    ecs.create_cluster(clusterName="prod")
    td = ecs.register_task_definition(
        family="web",
        taskRoleArn=task_role,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="256",
        memory="512",
        containerDefinitions=[
            {
                "name": "web",
                "image": "nginx",
                "essential": True,
                "portMappings": [{"containerPort": 80}],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {"awslogs-group": group, "awslogs-region": REGION},
                },
            }
        ],
    )["taskDefinition"]["taskDefinitionArn"]
    service = ecs.create_service(
        cluster="prod",
        serviceName="web",
        taskDefinition=td,
        desiredCount=2,
        launchType="FARGATE",
        loadBalancers=[{"targetGroupArn": tg, "containerName": "web", "containerPort": 80}],
        networkConfiguration={
            "awsvpcConfiguration": {"subnets": subnets, "securityGroups": [app_sg]}
        },
    )["service"]["serviceArn"]

    rds = boto3.client("rds", region_name=REGION)
    db = rds.create_db_instance(
        DBInstanceIdentifier="orders-db",
        Engine="postgres",
        DBInstanceClass="db.t4g.micro",
        AllocatedStorage=20,
        MasterUsername="appadmin",
        MasterUserPassword="moto-test-only-pw",
        VpcSecurityGroupIds=[db_sg],
    )["DBInstance"]["DBInstanceArn"]

    sqs = boto3.client("sqs", region_name=REGION)
    q_url = sqs.create_queue(QueueName="jobs")["QueueUrl"]
    q_arn = sqs.get_queue_attributes(QueueUrl=q_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    lam = boto3.client("lambda", region_name=REGION)
    fn = lam.create_function(
        FunctionName="worker",
        Runtime="python3.12",
        Role=fn_role,
        Handler="index.handler",
        Code={"ZipFile": _zip()},
    )["FunctionArn"]
    lam.create_event_source_mapping(EventSourceArn=q_arn, FunctionName="worker", BatchSize=10)

    _seed_telemetry(t0, alb, tg, group)
    return Env(alb, tg, service, db, db_sg, app_sg, fn, q_arn, task_role, group, t0)


def _seed_telemetry(t0: datetime, alb_arn: str, tg_arn: str, group: str) -> None:
    cw = boto3.client("cloudwatch", region_name=REGION)
    lb_dim = alb_arn.split(":loadbalancer/")[1]
    tg_dim = tg_arn.split(":")[-1]
    data = []
    for i in range(10):
        ts = t0 + timedelta(minutes=i)
        data += [
            {
                "MetricName": "HTTPCode_Target_5XX_Count",
                "Dimensions": [{"Name": "LoadBalancer", "Value": lb_dim}],
                "Timestamp": ts,
                "Value": 2.0 if i < 5 else 400.0,
            },
            {
                "MetricName": "UnHealthyHostCount",
                "Dimensions": [
                    {"Name": "LoadBalancer", "Value": lb_dim},
                    {"Name": "TargetGroup", "Value": tg_dim},
                ],
                "Timestamp": ts,
                "Value": 0.0 if i < 5 else 2.0,
            },
        ]
    cw.put_metric_data(Namespace="AWS/ApplicationELB", MetricData=data)
    cw.put_metric_data(
        Namespace="AWS/RDS",
        MetricData=[
            {
                "MetricName": "DatabaseConnections",
                "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "orders-db"}],
                "Timestamp": t0 + timedelta(minutes=i),
                "Value": 60.0 + 4 * i,
            }
            for i in range(10)
        ],
    )
    logs = boto3.client("logs", region_name=REGION)
    ms = int(t0.timestamp() * 1000)
    logs.put_log_events(
        logGroupName=group,
        logStreamName="web/web/abc",
        logEvents=[
            {"timestamp": ms + 60_000, "message": "INFO GET /health 200"},
            {
                "timestamp": ms + 300_000,
                "message": "ERROR OperationalError: too many clients already",
            },
            {"timestamp": ms + 310_000, "message": "WARN retrying connection password=hunter2"},
        ],
    )
