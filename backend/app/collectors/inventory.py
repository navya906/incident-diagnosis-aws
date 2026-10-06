"""Resource inventory and relationship discovery for the dependency graph.

Starts from seed ARNs (typically the alarmed resource) and walks outward, bounded by
`max_resources`:

  ALB -> target groups -> ECS services / EC2 instances / Lambda functions   (routes_to)
  ECS service / Lambda / EC2 -> IAM role                                     (assumes_role)
  Lambda -> SQS queue via event source mapping                              (polls)
  RDS -> its security groups                                                 (secured_by)
  compute -> RDS when an RDS security group admits the compute's SG          (connects_to)

The SG-based `connects_to` edge is an inference from network rules, not observed traffic.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from app.collectors.aws_common import AwsClients, call_with_backoff, error_code, paginate
from app.collectors.resources import AwsResource, ResourceIndex, canonical_id_from_arn, parse_arn
from app.offline.models import RelationshipRecord, ResourceRecord

log = logging.getLogger(__name__)

IAM_ACTIONS = frozenset(
    {
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "ecs:ListClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "rds:DescribeDBInstances",
        "lambda:GetFunctionConfiguration",
        "lambda:ListEventSourceMappings",
        "ec2:DescribeInstances",
        "ec2:DescribeSecurityGroups",
        "iam:GetRole",
        "iam:GetInstanceProfile",
        "sqs:GetQueueUrl",
        "sts:GetCallerIdentity",
    }
)


@dataclass
class Inventory:
    resources: list[AwsResource] = field(default_factory=list)
    relationships: list[RelationshipRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def index(self) -> ResourceIndex:
        return ResourceIndex(self.resources)

    def records(self) -> tuple[list[ResourceRecord], list[RelationshipRecord]]:
        return [r.to_record() for r in self.resources], list(self.relationships)


class InventoryDiscovery:
    def __init__(self, clients: AwsClients, max_resources: int = 100, max_db_scan: int = 200):
        self.clients = clients
        self.region = clients.region
        self.max_resources = max_resources
        self.max_db_scan = max_db_scan
        self._account: str | None = None
        self._tg_to_services: dict[str, list[str]] | None = None

    # ------------------------------------------------------------------ public
    def discover(self, seed_arns: list[str]) -> Inventory:
        inv = Inventory()
        found: dict[str, AwsResource] = {}
        edges: set[tuple[str, str, str]] = set()
        queue: deque[str] = deque()
        for arn in seed_arns:
            self._account = self._account or parse_arn(arn)["account"] or None
            queue.append(arn)
        visited: set[str] = set()
        while queue and len(found) < self.max_resources:
            arn = queue.popleft()
            if arn in visited:
                continue
            visited.add(arn)
            try:
                res, neighbors = self._describe(arn)
            except Exception as exc:  # one bad resource must not abort discovery
                inv.warnings.append(
                    f"inventory: could not describe {arn}: {error_code(exc) or exc}"
                )
                continue
            if res is None:
                continue
            found[res.resource_id] = res
            for target_arn, relation, outgoing in neighbors:
                try:
                    target_id = canonical_id_from_arn(target_arn)
                except ValueError:
                    inv.warnings.append(f"inventory: unsupported neighbor {target_arn}")
                    continue
                src, dst = (
                    (res.resource_id, target_id) if outgoing else (target_id, res.resource_id)
                )
                edges.add((src, dst, relation))
                if target_arn not in visited:
                    queue.append(target_arn)
        if queue:
            inv.warnings.append(f"inventory: stopped at max_resources={self.max_resources}")
        self._link_databases(found, edges, inv)
        inv.resources = sorted(found.values(), key=lambda r: r.resource_id)
        inv.relationships = [
            RelationshipRecord(source_id=s, target_id=t, relation_type=k)
            for s, t, k in sorted(edges)
            if s in found and t in found
        ]
        return inv

    # ------------------------------------------------------------------ helpers
    def _c(self, service: str):
        return self.clients.client(service)

    def account(self) -> str:
        if not self._account:
            self._account = self._c("sts").get_caller_identity()["Account"]
        return self._account

    def _arn(self, service: str, resource: str, region: str | None = None) -> str:
        region = self.region if region is None else region
        return f"arn:aws:{service}:{region}:{self.account()}:{resource}"

    def _sg_arn(self, sg_id: str) -> str:
        return self._arn("ec2", f"security-group/{sg_id}")

    def _describe(self, arn: str) -> tuple[AwsResource | None, list[tuple[str, str, bool]]]:
        a = parse_arn(arn)
        service, res = a["service"], a["resource"]
        if service == "elasticloadbalancing" and res.startswith("loadbalancer/"):
            return self._alb(arn)
        if service == "ecs" and res.startswith("service/"):
            return self._ecs_service(arn)
        if service == "rds" and res.startswith("db:"):
            return self._rds(arn)
        if service == "lambda" and res.startswith("function:"):
            return self._lambda(arn)
        if service == "ec2" and res.startswith("instance/"):
            return self._ec2(arn)
        if service == "ec2" and res.startswith("security-group/"):
            return self._sg(arn)
        if service == "sqs":
            return self._sqs(arn)
        if service == "iam" and res.startswith("role/"):
            return self._role(arn)
        raise ValueError(f"unsupported resource type for discovery: {arn}")

    def _base(self, arn: str, **kw) -> AwsResource:
        rid = canonical_id_from_arn(arn)
        name = kw.pop("name", rid.rsplit("/", 1)[-1])
        names = kw.pop("cloudtrail_names", [arn, name])
        return AwsResource(
            resource_id=rid,
            arn=arn,
            name=name,
            region=parse_arn(arn)["region"] or self.region,
            cloudtrail_names=list(dict.fromkeys(names)),
            **kw,
        )

    # ------------------------------------------------------------------ per type
    def _alb(self, arn: str):
        elb = self._c("elbv2")
        lb = call_with_backoff(elb.describe_load_balancers, LoadBalancerArns=[arn])[
            "LoadBalancers"
        ][0]
        lb_dim = arn.split(":loadbalancer/")[1]
        tgs = call_with_backoff(elb.describe_target_groups, LoadBalancerArn=arn)["TargetGroups"]
        per_tg, neighbors = [], []
        for tg in tgs:
            tg_arn = tg["TargetGroupArn"]
            per_tg.append({"LoadBalancer": lb_dim, "TargetGroup": tg_arn.split(":")[-1]})
            for svc_arn in self._services_for_target_group(tg_arn):
                neighbors.append((svc_arn, "routes_to", True))
            if tg.get("TargetType") in ("instance", "lambda"):
                health = call_with_backoff(elb.describe_target_health, TargetGroupArn=tg_arn)
                for d in health.get("TargetHealthDescriptions", []):
                    tid = d["Target"]["Id"]
                    target = tid if tid.startswith("arn:") else self._arn("ec2", f"instance/{tid}")
                    neighbors.append((target, "routes_to", True))
        res = self._base(
            arn,
            name=lb["LoadBalancerName"],
            resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
            service="elasticloadbalancing",
            kind="alb",
            dimensions={"default": [{"LoadBalancer": lb_dim}], "per_target_group": per_tg},
            config_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
            config_id=arn,
            security_groups=lb.get("SecurityGroups", []),
            attributes={"scheme": lb.get("Scheme"), "dns_name": lb.get("DNSName")},
        )
        return res, neighbors

    def _services_for_target_group(self, tg_arn: str) -> list[str]:
        if self._tg_to_services is None:
            self._tg_to_services = {}
            ecs = self._c("ecs")
            for page in paginate(ecs.list_clusters, "nextToken", "nextToken"):
                for cluster in page.get("clusterArns", []):
                    arns = [
                        s
                        for p in paginate(
                            ecs.list_services, "nextToken", "nextToken", cluster=cluster
                        )
                        for s in p.get("serviceArns", [])
                    ]
                    for i in range(0, len(arns), 10):
                        desc = call_with_backoff(
                            ecs.describe_services, cluster=cluster, services=arns[i : i + 10]
                        )
                        for svc in desc.get("services", []):
                            for lb in svc.get("loadBalancers", []):
                                if lb.get("targetGroupArn"):
                                    self._tg_to_services.setdefault(
                                        lb["targetGroupArn"], []
                                    ).append(svc["serviceArn"])
        return self._tg_to_services.get(tg_arn, [])

    def _ecs_service(self, arn: str):
        parts = parse_arn(arn)["resource"].split("/")
        cluster = parts[1] if len(parts) == 3 else "default"
        ecs = self._c("ecs")
        svc = call_with_backoff(ecs.describe_services, cluster=cluster, services=[arn])["services"][
            0
        ]
        td = call_with_backoff(ecs.describe_task_definition, taskDefinition=svc["taskDefinition"])[
            "taskDefinition"
        ]
        groups = [
            c["logConfiguration"]["options"]["awslogs-group"]
            for c in td.get("containerDefinitions", [])
            if c.get("logConfiguration", {}).get("logDriver") == "awslogs"
            and "awslogs-group" in c["logConfiguration"].get("options", {})
        ]
        sgs = (
            svc.get("networkConfiguration", {}).get("awsvpcConfiguration", {}).get("securityGroups")
            or []
        )
        neighbors = []
        if td.get("taskRoleArn"):
            neighbors.append((td["taskRoleArn"], "assumes_role", True))
        cluster_name = cluster.split("/")[-1]
        res = self._base(
            arn,
            name=svc["serviceName"],
            resource_type="AWS::ECS::Service",
            service="ecs",
            kind="ecs_service",
            dimensions={
                "default": [{"ClusterName": cluster_name, "ServiceName": svc["serviceName"]}]
            },
            log_groups=sorted(set(groups)),
            config_type="AWS::ECS::Service",
            config_id=arn,
            security_groups=sgs,
            attributes={
                "cluster": cluster_name,
                "task_definition": svc["taskDefinition"],
                "desired_count": svc.get("desiredCount"),
                "launch_type": svc.get("launchType"),
            },
        )
        return res, neighbors

    def _rds(self, arn: str, db: dict | None = None):
        ident = parse_arn(arn)["resource"][3:]
        if db is None:
            db = call_with_backoff(
                self._c("rds").describe_db_instances, DBInstanceIdentifier=ident
            )["DBInstances"][0]
        sgs = [g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])]
        engine = db.get("Engine", "")
        log_suffix = (
            "postgresql"
            if "postgres" in engine
            else "error"
            if engine in ("mysql", "mariadb")
            else None
        )
        res = self._base(
            arn,
            name=ident,
            resource_type="AWS::RDS::DBInstance",
            service="rds",
            kind="rds",
            dimensions={"default": [{"DBInstanceIdentifier": ident}]},
            log_groups=[f"/aws/rds/instance/{ident}/{log_suffix}"] if log_suffix else [],
            config_type="AWS::RDS::DBInstance",
            config_id=db.get("DbiResourceId"),
            security_groups=sgs,
            attributes={"engine": engine, "instance_class": db.get("DBInstanceClass")},
        )
        return res, [(self._sg_arn(g), "secured_by", True) for g in sgs]

    def _lambda(self, arn: str):
        name = parse_arn(arn)["resource"].split(":")[1]
        lam = self._c("lambda")
        cfg = call_with_backoff(lam.get_function_configuration, FunctionName=name)
        neighbors = [(cfg["Role"], "assumes_role", True)] if cfg.get("Role") else []
        for page in paginate(
            lam.list_event_source_mappings, "Marker", "NextMarker", FunctionName=name
        ):
            for m in page.get("EventSourceMappings", []):
                src = m.get("EventSourceArn", "")
                if src.startswith("arn:aws:sqs:"):
                    neighbors.append((src, "polls", True))
        group = (cfg.get("LoggingConfig") or {}).get("LogGroup") or f"/aws/lambda/{name}"
        res = self._base(
            arn,
            name=name,
            resource_type="AWS::Lambda::Function",
            service="lambda",
            kind="lambda",
            dimensions={"default": [{"FunctionName": name}]},
            log_groups=[group],
            config_type="AWS::Lambda::Function",
            config_id=name,
            security_groups=(cfg.get("VpcConfig") or {}).get("SecurityGroupIds", []),
            attributes={"timeout_seconds": cfg.get("Timeout"), "runtime": cfg.get("Runtime")},
        )
        return res, neighbors

    def _ec2(self, arn: str):
        iid = parse_arn(arn)["resource"].split("/")[1]
        inst = call_with_backoff(self._c("ec2").describe_instances, InstanceIds=[iid])[
            "Reservations"
        ][0]["Instances"][0]
        neighbors = []
        profile = (inst.get("IamInstanceProfile") or {}).get("Arn")
        if profile:
            pname = profile.split("/")[-1]
            roles = call_with_backoff(
                self._c("iam").get_instance_profile, InstanceProfileName=pname
            )["InstanceProfile"]["Roles"]
            neighbors += [(r["Arn"], "assumes_role", True) for r in roles]
        res = self._base(
            arn,
            name=iid,
            cloudtrail_names=[iid, arn],
            resource_type="AWS::EC2::Instance",
            service="ec2",
            kind="ec2",
            dimensions={"default": [{"InstanceId": iid}]},
            config_type="AWS::EC2::Instance",
            config_id=iid,
            security_groups=[g["GroupId"] for g in inst.get("SecurityGroups", [])],
            attributes={"instance_type": inst.get("InstanceType")},
        )
        return res, neighbors

    def _sg(self, arn: str):
        sg_id = parse_arn(arn)["resource"].split("/")[1]
        sg = call_with_backoff(self._c("ec2").describe_security_groups, GroupIds=[sg_id])[
            "SecurityGroups"
        ][0]
        sources = sorted(
            {
                pair["GroupId"]
                for perm in sg.get("IpPermissions", [])
                for pair in perm.get("UserIdGroupPairs", [])
            }
        )
        res = self._base(
            arn,
            name=sg_id,
            cloudtrail_names=[sg_id],
            resource_type="AWS::EC2::SecurityGroup",
            service="ec2",
            kind="sg",
            config_type="AWS::EC2::SecurityGroup",
            config_id=sg_id,
            attributes={"group_name": sg.get("GroupName"), "ingress_from_groups": sources},
        )
        return res, []

    def _sqs(self, arn: str):
        name = parse_arn(arn)["resource"]
        names = [arn, name]
        try:
            url = call_with_backoff(self._c("sqs").get_queue_url, QueueName=name)["QueueUrl"]
            names.append(url)
        except Exception as exc:
            if error_code(exc) not in (
                "AWS.SimpleQueueService.NonExistentQueue",
                "QueueDoesNotExist",
            ):
                raise
        res = self._base(
            arn,
            name=name,
            cloudtrail_names=names,
            resource_type="AWS::SQS::Queue",
            service="sqs",
            kind="sqs",
            dimensions={"default": [{"QueueName": name}]},
        )
        return res, []

    def _role(self, arn: str):
        name = parse_arn(arn)["resource"].split("/")[-1]
        role = call_with_backoff(self._c("iam").get_role, RoleName=name)["Role"]
        res = self._base(
            arn,
            name=name,
            cloudtrail_names=[name, arn],
            resource_type="AWS::IAM::Role",
            service="iam",
            kind="iam_role",
            config_type="AWS::IAM::Role",
            config_id=role.get("RoleId"),
        )
        return res, []

    def _link_databases(
        self, found: dict[str, AwsResource], edges: set[tuple[str, str, str]], inv: Inventory
    ) -> None:
        """Add `connects_to` edges from compute to RDS instances whose SG admits the compute SG."""
        compute = [r for r in found.values() if r.kind in ("ecs_service", "lambda", "ec2")]
        if not compute:
            return
        scanned = 0
        for page in paginate(self._c("rds").describe_db_instances, "Marker", "Marker"):
            for db in page.get("DBInstances", []):
                scanned += 1
                if scanned > self.max_db_scan:
                    inv.warnings.append(f"inventory: stopped RDS scan at {self.max_db_scan}")
                    return
                db_sgs = [g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])]
                admitted: set[str] = set()
                if db_sgs:
                    desc = call_with_backoff(
                        self._c("ec2").describe_security_groups, GroupIds=db_sgs
                    )
                    for sg in desc.get("SecurityGroups", []):
                        for perm in sg.get("IpPermissions", []):
                            admitted |= {p["GroupId"] for p in perm.get("UserIdGroupPairs", [])}
                users = [c for c in compute if admitted & set(c.security_groups)]
                if not users or len(found) >= self.max_resources:
                    continue
                arn = db["DBInstanceArn"]
                db_res, neighbors = self._rds(arn, db)
                found.setdefault(db_res.resource_id, db_res)
                for c in users:
                    edges.add((c.resource_id, db_res.resource_id, "connects_to"))
                for sg_arn, relation, _ in neighbors:
                    sg_res, _ = self._sg(sg_arn)
                    found.setdefault(sg_res.resource_id, sg_res)
                    edges.add((db_res.resource_id, sg_res.resource_id, relation))
