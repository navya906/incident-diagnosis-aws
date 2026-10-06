"""AWS Config resource history. Optional: accounts without a configuration recorder are skipped."""

from __future__ import annotations

import logging

from app.collectors.aws_common import AwsClients, error_code, paginate
from app.collectors.resources import ResourceIndex
from app.contracts.events import CanonicalEvent, EventSource, Severity
from app.interfaces.collector import CollectionRequest, Collector

log = logging.getLogger(__name__)
_NO_RECORDER = {"NoAvailableConfigurationRecorderException", "NoSuchConfigurationRecorderException"}
_SKIP = {"ResourceNotDiscoveredException", "ValidationException", "InvalidParameterValueException"}


class ConfigHistoryCollector(Collector):
    name = "aws_config"
    IAM_ACTIONS = frozenset({"config:GetResourceConfigHistory"})

    def __init__(self, clients: AwsClients, index: ResourceIndex):
        self.clients, self.index = clients, index
        self.warnings: list[str] = []
        self.recorder_available = True

    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        self.warnings = []
        if not self.clients.settings.config_enabled:
            return []
        if request.sources is not None and EventSource.AWS_CONFIG not in request.sources:
            return []
        cfg = self.clients.client("config")
        events: list[CanonicalEvent] = []
        for rid in request.resource_ids:
            if not self.recorder_available:
                break
            res = self.index.get(rid)
            if res is None:
                self.warnings.append(f"config: unknown resource {rid}")
                continue
            if not (res.config_type and res.config_id):
                continue
            try:
                for page in paginate(
                    cfg.get_resource_config_history,
                    "nextToken",
                    "nextToken",
                    resourceType=res.config_type,
                    resourceId=res.config_id,
                    earlierTime=request.window_start,
                    laterTime=request.window_end,
                    chronologicalOrder="Forward",
                    limit=100,
                ):
                    for item in page.get("configurationItems", []):
                        ts = item.get("configurationItemCaptureTime")
                        if ts is None or not request.window_start <= ts <= request.window_end:
                            continue
                        status = item.get("configurationItemStatus", "OK")
                        state = str(item.get("configurationStateId", ""))
                        name = item.get("resourceName") or res.name
                        events.append(
                            CanonicalEvent.build(
                                timestamp=ts,
                                source=EventSource.AWS_CONFIG,
                                service=res.service,
                                resource_id=rid,
                                event_type="ConfigurationItemChange",
                                severity=Severity.WARNING
                                if status.startswith("ResourceDeleted")
                                else Severity.INFO,
                                message=f"{res.config_type} {name} configuration recorded "
                                f"({status})",
                                metadata={
                                    "resourceType": res.config_type,
                                    "configurationItemStatus": status,
                                    "configurationStateId": state,
                                },
                                raw_ref=f"cfg:{res.config_type}:{res.config_id}:{state}",
                            )
                        )
            except Exception as exc:
                code = error_code(exc)
                if code in _NO_RECORDER:
                    self.recorder_available = False
                    self.warnings.append(
                        "config: no configuration recorder; Config history skipped"
                    )
                    break
                if code in _SKIP:
                    self.warnings.append(f"config: {rid} not available ({code})")
                    continue
                raise
        return sorted(events, key=lambda e: (e.timestamp, e.event_id))
