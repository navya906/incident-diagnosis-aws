"""CloudWatch metrics via batched GetMetricData, driven by the incident's resources and window."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.collectors.aws_common import AwsClients, paginate
from app.collectors.resources import AwsResource, ResourceIndex
from app.contracts.events import CanonicalEvent, EventSource
from app.interfaces.collector import CollectionRequest, Collector

log = logging.getLogger(__name__)
MAX_QUERIES_PER_CALL = 500


@dataclass(frozen=True)
class MetricDef:
    namespace: str
    name: str
    stat: str
    unit: str
    dims_key: str = "default"  # which dimension set of the resource to use


_ALB = "AWS/ApplicationELB"
METRIC_CATALOG: dict[str, list[MetricDef]] = {
    "alb": [
        MetricDef(_ALB, "RequestCount", "Sum", "Count"),
        MetricDef(_ALB, "HTTPCode_Target_5XX_Count", "Sum", "Count"),
        MetricDef(_ALB, "HTTPCode_ELB_5XX_Count", "Sum", "Count"),
        MetricDef(_ALB, "TargetResponseTime", "Average", "Seconds"),
        MetricDef(_ALB, "UnHealthyHostCount", "Average", "Count", dims_key="per_target_group"),
    ],
    "ecs_service": [
        MetricDef("AWS/ECS", "CPUUtilization", "Average", "Percent"),
        MetricDef("AWS/ECS", "MemoryUtilization", "Average", "Percent"),
        MetricDef("ECS/ContainerInsights", "RunningTaskCount", "Average", ""),
    ],
    "rds": [
        MetricDef("AWS/RDS", "CPUUtilization", "Average", "Percent"),
        MetricDef("AWS/RDS", "DatabaseConnections", "Average", "Count"),
        MetricDef("AWS/RDS", "ReadLatency", "Average", "Seconds"),
        MetricDef("AWS/RDS", "WriteLatency", "Average", "Seconds"),
        MetricDef("AWS/RDS", "DiskQueueDepth", "Average", "Count"),
        MetricDef("AWS/RDS", "ReadIOPS", "Average", "Count/Second"),
    ],
    "lambda": [
        MetricDef("AWS/Lambda", "Invocations", "Sum", ""),
        MetricDef("AWS/Lambda", "Errors", "Sum", ""),
        MetricDef("AWS/Lambda", "Duration", "Average", "Milliseconds"),
        MetricDef("AWS/Lambda", "Throttles", "Sum", ""),
        MetricDef("AWS/Lambda", "ConcurrentExecutions", "Maximum", ""),
    ],
    "ec2": [
        MetricDef("AWS/EC2", "CPUUtilization", "Average", "Percent"),
        MetricDef("AWS/EC2", "StatusCheckFailed", "Maximum", ""),
    ],
    "sqs": [
        MetricDef("AWS/SQS", "ApproximateNumberOfMessagesVisible", "Average", ""),
        MetricDef("AWS/SQS", "ApproximateAgeOfOldestMessage", "Maximum", "Seconds"),
        MetricDef("AWS/SQS", "NumberOfMessagesSent", "Sum", ""),
        MetricDef("AWS/SQS", "NumberOfMessagesDeleted", "Sum", ""),
    ],
}


def choose_period(start: datetime, now: datetime | None = None) -> int:
    """Finest resolution CloudWatch still retains for data starting at `start`."""
    age = (now or datetime.now(UTC)) - start
    if age < timedelta(days=15):
        return 60
    if age < timedelta(days=63):
        return 300
    return 3600


@dataclass(frozen=True)
class _Query:
    qid: str
    resource: AwsResource
    metric: MetricDef
    dims: tuple[tuple[str, str], ...]


class CloudWatchMetricsCollector(Collector):
    name = "cloudwatch_metrics"
    IAM_ACTIONS = frozenset({"cloudwatch:GetMetricData"})

    def __init__(self, clients: AwsClients, index: ResourceIndex):
        self.clients, self.index = clients, index
        self.warnings: list[str] = []

    def _queries(self, request: CollectionRequest) -> list[_Query]:
        queries: list[_Query] = []
        for rid in request.resource_ids:
            res = self.index.get(rid)
            if res is None:
                self.warnings.append(f"metrics: unknown resource {rid}")
                continue
            for m in METRIC_CATALOG.get(res.kind, []):
                for dims in res.dimensions.get(m.dims_key, []):
                    qid = f"q{len(queries)}"
                    queries.append(_Query(qid, res, m, tuple(sorted(dims.items()))))
        return queries

    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        self.warnings = []
        if request.sources is not None and EventSource.CLOUDWATCH_METRIC not in request.sources:
            return []
        queries = self._queries(request)
        if not queries:
            return []
        period = self.clients.settings.metrics_period_seconds or choose_period(request.window_start)
        cw = self.clients.client("cloudwatch")
        # (resource_id, metric) -> timestamp -> summed value (several dimension sets are summed)
        values: dict[tuple[str, MetricDef], dict[datetime, float]] = defaultdict(dict)
        by_id = {q.qid: q for q in queries}
        for i in range(0, len(queries), MAX_QUERIES_PER_CALL):
            batch = queries[i : i + MAX_QUERIES_PER_CALL]
            spec = [
                {
                    "Id": q.qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": q.metric.namespace,
                            "MetricName": q.metric.name,
                            "Dimensions": [{"Name": k, "Value": v} for k, v in q.dims],
                        },
                        "Period": period,
                        "Stat": q.metric.stat,
                    },
                    "ReturnData": True,
                }
                for q in batch
            ]
            for page in paginate(
                cw.get_metric_data,
                "NextToken",
                "NextToken",
                MetricDataQueries=spec,
                StartTime=request.window_start,
                EndTime=request.window_end,
                ScanBy="TimestampAscending",
            ):
                for result in page.get("MetricDataResults", []):
                    q = by_id[result["Id"]]
                    if result.get("StatusCode") == "Forbidden":
                        rid = q.resource.resource_id
                        self.warnings.append(f"metrics: access denied for {q.metric.name} on {rid}")
                    series = values[(q.resource.resource_id, q.metric)]
                    for ts, v in zip(
                        result.get("Timestamps", []), result.get("Values", []), strict=False
                    ):
                        t = ts.astimezone(UTC)
                        series[t] = series.get(t, 0.0) + float(v)
        events = []
        for (rid, m), series in values.items():
            res = self.index.get(rid)
            for t, v in series.items():
                if not request.window_start <= t <= request.window_end:
                    continue
                events.append(
                    CanonicalEvent.build(
                        timestamp=t,
                        source=EventSource.CLOUDWATCH_METRIC,
                        service=res.service,
                        resource_id=rid,
                        event_type="metric_datapoint",
                        metric=m.name,
                        value=round(v, 6),
                        metadata={
                            "namespace": m.namespace,
                            "stat": m.stat,
                            "period_seconds": period,
                            "unit": m.unit,
                        },
                        raw_ref=f"cw:{m.namespace}/{m.name}/{rid}",
                    )
                )
        return sorted(events, key=lambda e: (e.timestamp, e.event_id))
