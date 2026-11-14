"""Loaders used by the cache layer.

``StaticLoader`` answers immediately from a prepared table.
``ControllableLoader`` blocks each load until the driver explicitly
completes or fails it, so tests and the CLI demo can reproduce exact
interleavings of concurrent queries, revocations and late old responses.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Tuple

from cache_layer import LoadResult, RefreshError


class StaticLoader:
    """Serves prepared LoadResults keyed by (resource_id, view)."""

    def __init__(self, results=None):
        self.results: Dict[Tuple[str, str], object] = dict(results or {})
        self.calls: List[Tuple[str, str, str, str]] = []
        self._lock = threading.Lock()

    def loader(self, identity, resource_id, view) -> LoadResult:
        with self._lock:
            self.calls.append((identity.tenant, identity.principal, resource_id, view))
        outcome = self.results[(resource_id, view)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)


class _Gate:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result = None
        self.error = None


class ControllableLoader:
    """A loader whose completion order is fully controlled by the driver.

    Each expected load is bound to a token via :meth:`loader_for`.  The
    produced loader blocks until :meth:`complete` or :meth:`fail` is called
    for that token, letting the driver interleave loads, revocations and
    late responses deterministically.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._lock = threading.Lock()
        self._gates: Dict[str, _Gate] = {}
        self.started: List[Tuple[str, str, str]] = []  # (token, principal, view)

    def _gate(self, token: str) -> _Gate:
        with self._lock:
            return self._gates.setdefault(token, _Gate())

    def loader_for(self, token: str):
        gate = self._gate(token)

        def _load(identity, resource_id, view) -> LoadResult:
            with self._lock:
                self.started.append((token, identity.principal, view))
            if not gate.event.wait(self._timeout):
                raise RefreshError(f"gate {token!r} was not released in time")
            if gate.error is not None:
                raise gate.error
            return gate.result

        return _load

    def wait_started(self, token: str, timeout: float = 5.0) -> bool:
        """Block until a load for ``token`` has actually started."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if any(t == token for t, _, _ in self.started):
                    return True
            time.sleep(0.005)
        return False

    def complete(self, token: str, result: LoadResult) -> None:
        gate = self._gate(token)
        gate.result = result
        gate.event.set()

    def fail(self, token: str, error: Exception) -> None:
        gate = self._gate(token)
        gate.error = error
        gate.event.set()
