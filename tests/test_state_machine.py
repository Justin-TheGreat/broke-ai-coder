from __future__ import annotations

import itertools

import pytest

from app.orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    TaskStatus,
    can_transition,
    ensure_transition,
)

PAIRS = list(itertools.product(list(TaskStatus), list(TaskStatus)))


@pytest.mark.parametrize(("a", "b"), PAIRS)
def test_every_pair(a, b):
    if b in ALLOWED_TRANSITIONS[a]:
        ensure_transition(a, b)
        assert can_transition(a, b)
    else:
        assert not can_transition(a, b)
        with pytest.raises(InvalidTransition) as ei:
            ensure_transition(a, b)
        assert ei.value.from_status == a
        assert ei.value.to_status == b


def test_terminal_have_no_exits():
    for s in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[s] == frozenset()


def test_table_immutable():
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[TaskStatus.QUEUED] = frozenset()  # type: ignore[index]


def test_key_transitions():
    assert can_transition(TaskStatus.RUNNING, TaskStatus.ROUTING)
    assert not can_transition(TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert not can_transition(TaskStatus.RUNNING, TaskStatus.RUNNING)
