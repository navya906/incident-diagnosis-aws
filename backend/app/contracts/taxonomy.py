"""Closed root-cause taxonomy. Changing this set is a contract change (see DECISIONS.md)."""

from enum import StrEnum


class RootCause(StrEnum):
    DEPLOYMENT_FAILURE = "deployment_failure"
    CONNECTION_EXHAUSTION = "connection_exhaustion"
    FUNCTION_TIMEOUT = "function_timeout"
    CPU_SATURATION = "cpu_saturation"
    LB_ERROR_SPIKE = "lb_error_spike"
    IAM_PERMISSION_FAILURE = "iam_permission_failure"
    SECURITY_GROUP_MISCONFIGURATION = "security_group_misconfiguration"
    DB_LATENCY_INCREASE = "db_latency_increase"
    TASK_CRASH_LOOP = "task_crash_loop"
    QUEUE_BACKLOG = "queue_backlog"
    OTHER = "other"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


#: The ten injectable fault types (everything except the two catch-all labels).
FAULT_TYPES: tuple[RootCause, ...] = tuple(
    c for c in RootCause if c not in (RootCause.OTHER, RootCause.INSUFFICIENT_EVIDENCE)
)
