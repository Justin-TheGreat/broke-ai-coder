from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    ROUTING = "ROUTING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

_T = TaskStatus
ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = MappingProxyType(
    {
        _T.QUEUED: frozenset({_T.ROUTING, _T.CANCELLED}),
        _T.ROUTING: frozenset({_T.RUNNING, _T.WAITING_APPROVAL, _T.FAILED, _T.CANCELLED}),
        _T.RUNNING: frozenset(
            {_T.ROUTING, _T.WAITING_APPROVAL, _T.SUCCEEDED, _T.FAILED, _T.CANCELLED}
        ),
        _T.WAITING_APPROVAL: frozenset({_T.RUNNING, _T.ROUTING, _T.FAILED, _T.CANCELLED}),
        _T.SUCCEEDED: frozenset(),
        _T.FAILED: frozenset(),
        _T.CANCELLED: frozenset(),
    }
)


class InvalidTransition(Exception):
    def __init__(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        super().__init__(f"invalid task transition {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
