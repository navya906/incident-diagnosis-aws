"""AWS resource model and canonical-id mapping shared by collectors and inventory discovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.offline.models import ResourceRecord

# Canonical ids match the simulator and app.contracts.events.CANONICAL_RESOURCE_ID.


class AwsResource(BaseModel):
    resource_id: str  # canonical, e.g. "rds/orders-db"
    arn: str | None = None
    resource_type: str  # CloudFormation-style, e.g. "AWS::RDS::DBInstance"
    service: str  # e.g. "rds"
    kind: str  # catalog key: alb | ecs_service | rds | lambda | ec2 | sqs | sg | iam_role
    name: str
    region: str
    #: CloudWatch dimension sets; several sets are summed (e.g. one per ALB target group).
    dimensions: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    log_groups: list[str] = Field(default_factory=list)
    cloudtrail_names: list[str] = Field(default_factory=list)
    config_type: str | None = None
    config_id: str | None = None
    security_groups: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    def to_record(self, role: str = "discovered") -> ResourceRecord:
        attrs = {"arn": self.arn, "region": self.region, **self.attributes}
        return ResourceRecord(
            resource_id=self.resource_id,
            resource_type=self.resource_type,
            service=self.service,
            role=role,
            attributes={k: v for k, v in attrs.items() if v is not None},
        )


class ResourceIndex:
    """Canonical id -> AwsResource. Collectors resolve CollectionRequest.resource_ids here."""

    def __init__(self, resources: list[AwsResource] | None = None):
        self._by_id: dict[str, AwsResource] = {}
        for r in resources or []:
            self.add(r)

    def add(self, resource: AwsResource) -> None:
        self._by_id[resource.resource_id] = resource

    def get(self, resource_id: str) -> AwsResource | None:
        return self._by_id.get(resource_id)

    def __contains__(self, resource_id: str) -> bool:
        return resource_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


def parse_arn(arn: str) -> dict[str, str]:
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        raise ValueError(f"not an ARN: {arn!r}")
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


def canonical_id_from_arn(arn: str) -> str:
    """Map an ARN to the canonical resource id used everywhere in the system."""
    a = parse_arn(arn)
    service, res = a["service"], a["resource"]
    if service == "elasticloadbalancing":
        if res.startswith("loadbalancer/"):
            return "alb/" + res.split("/")[2]
        if res.startswith("targetgroup/"):
            return "tg/" + res.split("/")[1]
    elif service == "ecs" and res.startswith("service/"):
        return "ecs/service/" + res.split("/")[-1]
    elif service == "rds" and res.startswith("db:"):
        return "rds/" + res[3:]
    elif service == "lambda" and res.startswith("function:"):
        return "lambda/function/" + res.split(":")[1]
    elif service == "ec2":
        if res.startswith("instance/"):
            return "ec2/instance/" + res.split("/")[1]
        if res.startswith("security-group/"):
            return "sg/" + res.split("/")[1]
    elif service == "sqs":
        return "sqs/queue/" + res
    elif service == "iam" and res.startswith("role/"):
        return "iam/role/" + res.split("/")[-1]
    elif service == "s3":
        return "s3/bucket/" + res.split("/")[0]
    raise ValueError(f"unsupported ARN for canonical id: {arn!r}")
