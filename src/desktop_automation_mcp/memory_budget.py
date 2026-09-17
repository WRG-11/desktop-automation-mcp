"""Bound concurrent, short-lived screenshot working memory.

This module does not estimate image content or retain buffers. It only owns
an atomic in-flight byte counter so concurrent captures cannot each assume
they have the whole process budget. Callers reserve a conservative estimate
before grabbing pixels and release it in `finally` via the context manager.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator

from .errors import MemoryBudgetExceededError


class InFlightMemoryBudget:
    def __init__(self, limit_bytes: int) -> None:
        if not isinstance(limit_bytes, int) or isinstance(limit_bytes, bool):
            raise ValueError("limit_bytes must be an integer")
        if limit_bytes <= 0:
            raise ValueError("limit_bytes must be positive")
        self.limit_bytes = limit_bytes
        self._in_use = 0
        self._lock = threading.Lock()

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @contextmanager
    def reserve(self, amount_bytes: int) -> Iterator[None]:
        if not isinstance(amount_bytes, int) or isinstance(amount_bytes, bool):
            raise ValueError("amount_bytes must be an integer")
        if amount_bytes <= 0:
            raise ValueError("amount_bytes must be positive")
        with self._lock:
            if self._in_use + amount_bytes > self.limit_bytes:
                raise MemoryBudgetExceededError(
                    "Concurrent screenshot memory budget exceeded: "
                    f"{self._in_use + amount_bytes} > {self.limit_bytes} bytes."
                )
            self._in_use += amount_bytes
        try:
            yield
        finally:
            with self._lock:
                self._in_use -= amount_bytes
