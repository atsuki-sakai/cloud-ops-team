"""Invocation-wide limits shared by isolated and parallel analysis stages."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

ISSUES_CREATED_STATE_KEY = "ops:issues_created"


@dataclass
class RunBudget:
    """Trusted counters; never populated from a previous session's state."""

    invocation_id: str
    issues_created: int = 0
    lock: Any = field(default_factory=Lock, repr=False)


_CURRENT_BUDGET: ContextVar[RunBudget | None] = ContextVar(
    "cloud_ops_run_budget", default=None
)


def current_run_budget() -> RunBudget | None:
    """Capture the current budget while constructing a stage's tool scope."""
    return _CURRENT_BUDGET.get()


@contextmanager
def invocation_budget(invocation_id: str) -> Iterator[RunBudget]:
    """Reuse a root budget on adapter re-entry, then reset at invocation exit."""
    previous = current_run_budget()
    if previous is not None and previous.invocation_id == invocation_id:
        yield previous
        return
    budget = RunBudget(invocation_id=invocation_id)
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)
