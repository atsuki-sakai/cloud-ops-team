"""Context-variable helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

T = TypeVar("T")


@contextmanager
def bind_var(var: ContextVar[T], value: T) -> Generator[None, None, None]:
    """Set ``var`` for the duration of the block and restore it afterwards."""
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)
