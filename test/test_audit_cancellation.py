"""
Tests for cooperative cancellation in the lazy `Auditor.audit` flow.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Optional

import pytest
from packaging.version import Version

from pip_audit._audit import (
    AuditCancelledError,
    AuditOptions,
    Auditor,
    CancellationToken,
)
from pip_audit._dependency_source.interface import DependencySource
from pip_audit._service.interface import (
    ResolvedDependency,
    ServiceError,
    VulnerabilityResult,
    VulnerabilityService,
)


def _dep(name: str, version: str = "1.0.0") -> ResolvedDependency:
    return ResolvedDependency(name=name, version=Version(version))


class RecordingIterator:
    def __init__(self, items, events, label, close_error=None):
        self._items = list(items)
        self._index = 0
        self._events = events
        self._label = label
        self._close_error = close_error
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._items):
            self._events.append(f"{self._label}:exhausted")
            raise StopIteration
        item = self._items[self._index]
        self._index += 1
        self._events.append(f"{self._label}:next:{item.name}")
        return item

    def close(self):
        self.closed = True
        self._events.append(f"{self._label}:close")
        if self._close_error is not None:
            raise self._close_error


class LazySource(DependencySource):
    def __init__(self, deps, events, close_error=None):
        self._deps = deps
        self._events = events
        self._close_error = close_error
        self.iterator: Optional[RecordingIterator] = None

    def collect(self):
        self.iterator = RecordingIterator(self._deps, self._events, "specs", self._close_error)
        return self.iterator

    def fix(self, _fix_version):
        raise NotImplementedError


class StubService(VulnerabilityService):
    def __init__(self, events):
        self._events = events

    def query(self, spec):
        self._events.append(f"service:query:{spec.name}")
        return spec, [
            VulnerabilityResult(
                id=f"VULN-{spec.name.upper()}",
                description="synthetic",
                fix_versions=[Version("2.0.0")],
                aliases=set(),
            )
        ]


class BatchService(VulnerabilityService):
    def __init__(self, events, close_error=None):
        self._events = events
        self._close_error = close_error
        self.generator_closed = False

    def query(self, spec):  # pragma: no cover
        raise NotImplementedError

    def query_all(self, specs):
        return self._generate(specs)

    def _generate(self, specs):
        try:
            for spec in specs:
                self._events.append(f"service:query_all:{spec.name}")
                yield spec, [
                    VulnerabilityResult(
                        id=f"BATCH-{spec.name.upper()}",
                        description="synthetic",
                        fix_versions=[Version("9.9.9")],
                        aliases=set(),
                    )
                ]
        finally:
            self.generator_closed = True
            self._events.append("service:query_all:close")
            if self._close_error is not None:
                raise self._close_error


class CountingToken(CancellationToken):
    def __init__(self, cancel_on_check=None):
        super().__init__()
        self.checks = 0
        self._cancel_on_check = cancel_on_check

    def is_cancelled(self):
        self.checks += 1
        if self._cancel_on_check is not None and self.checks >= self._cancel_on_check:
            self.cancel()
        return super().is_cancelled()


class CallbackBatchService(BatchService):
    """Custom query_all that requests cancellation while a next() is in flight."""

    def __init__(self, events, token):
        super().__init__(events)
        self._token = token

    def _generate(self, specs):
        try:
            for spec in specs:
                self._token.cancel()
                self._events.append(f"service:callback:{spec.name}")
                yield spec, []
        finally:
            self.generator_closed = True
            self._events.append("service:callback:close")


@pytest.fixture
def deps():
    return [_dep("pkg-a"), _dep("pkg-b"), _dep("pkg-c")]


def test_audit_without_token_keeps_default_behavior(deps):
    events = []
    results = list(Auditor(StubService(events)).audit(LazySource(deps, events)))
    assert [d.name for d, _ in results] == ["pkg-a", "pkg-b", "pkg-c"]
    assert events == [
        "specs:next:pkg-a",
        "service:query:pkg-a",
        "specs:next:pkg-b",
        "service:query:pkg-b",
        "specs:next:pkg-c",
        "service:query:pkg-c",
        "specs:exhausted",
        "specs:close",
    ]


def test_audit_uncancelled_token_unchanged(deps):
    events = []
    results = list(
        Auditor(StubService(events)).audit(LazySource(deps, events), CancellationToken())
    )
    assert [d.name for d, _ in results] == ["pkg-a", "pkg-b", "pkg-c"]


def test_precancelled_audit_never_pulls_source(deps):
    events = []
    source = LazySource(deps, events)
    token = CancellationToken()
    token.cancel()

    with pytest.raises(AuditCancelledError):
        next(Auditor(StubService(events)).audit(source, token))

    assert events == ["specs:close"]
    assert source.iterator is not None and source.iterator.closed


def test_cancel_during_inflight_next_completes_then_stops(deps):
    events = []
    source = LazySource(deps, events)
    token = CancellationToken()
    service = CallbackBatchService(events, token)

    gen = Auditor(service).audit(source, token)
    with pytest.raises(AuditCancelledError):
        next(gen)

    # The in-flight next() finished (callback + event recorded), but the
    # result was withheld and the source was not advanced a second time.
    assert events == [
        "specs:next:pkg-a",
        "service:callback:pkg-a",
        "service:callback:close",
        "specs:close",
    ]
    assert service.generator_closed
    assert source.iterator is not None and source.iterator.closed


def test_cancel_after_publish_retains_result_and_stops(deps):
    events = []
    token = CountingToken(cancel_on_check=3)
    gen = Auditor(StubService(events)).audit(LazySource(deps, events), token)

    first = next(gen)
    assert first[0].name == "pkg-a"

    with pytest.raises(AuditCancelledError):
        next(gen)
    with pytest.raises(StopIteration):
        next(gen)

    assert [e for e in events if not e.endswith(":close")] == [
        "specs:next:pkg-a",
        "service:query:pkg-a",
    ]


def test_audit_cancelled_error_distinct_from_service_error():
    assert not issubclass(AuditCancelledError, ServiceError)


def test_early_caller_close_uses_lifo_order(deps):
    events = []
    source = LazySource(deps, events)
    service = BatchService(events)
    gen = Auditor(service).audit(source)

    next(gen)
    gen.close()

    assert events == [
        "specs:next:pkg-a",
        "service:query_all:pkg-a",
        "service:query_all:close",
        "specs:close",
    ]
    assert service.generator_closed
    assert source.iterator is not None and source.iterator.closed


def test_teardown_errors_do_not_mask_service_error(deps, caplog):
    events = []
    original = ServiceError("upstream outage")
    source = LazySource(deps, events, close_error=RuntimeError("specs-close-boom"))

    class FailingCloseService(VulnerabilityService):
        def query(self, spec):  # pragma: no cover
            raise NotImplementedError

        def query_all(self, specs):
            inner = self._generate(specs)

            class _Iterator:
                def __iter__(self):
                    return self

                def __next__(self):
                    return next(inner)

                def close(self):
                    events.append("service:failing:close")
                    raise RuntimeError("service-close-boom")

            return _Iterator()

        def _generate(self, specs):
            for index, spec in enumerate(specs):
                events.append(f"service:failing:{spec.name}")
                if index == 1:
                    raise original
                yield spec, []

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ServiceError, match="upstream outage"):
            list(Auditor(FailingCloseService()).audit(source))

    assert [e for e in events if e.endswith(":close")] == [
        "service:failing:close",
        "specs:close",
    ]
    assert "service-close-boom" in caplog.text
    assert "specs-close-boom" in caplog.text


def test_cancellation_error_beats_teardown_errors(deps):
    events = []
    source = LazySource(deps, events, close_error=RuntimeError("specs-close-boom"))
    service = BatchService(events, close_error=RuntimeError("service-close-boom"))
    token = CancellationToken()
    token.cancel()

    with pytest.raises(AuditCancelledError):
        next(Auditor(service).audit(source, token))


def test_dry_run_without_token_drains_source(deps, caplog):
    events = []
    source = LazySource(deps, events)
    with caplog.at_level(logging.INFO):
        results = list(
            Auditor(StubService(events), AuditOptions(dry_run=True)).audit(source)
        )

    assert results == []
    assert [e for e in events if not e.endswith(":close")] == [
        "specs:next:pkg-a",
        "specs:next:pkg-b",
        "specs:next:pkg-c",
        "specs:exhausted",
    ]
    assert source.iterator is not None and source.iterator.closed
    assert "would have audited 3 packages" in caplog.text


def test_dry_run_precancelled(deps):
    events = []
    source = LazySource(deps, events)
    token = CancellationToken()
    token.cancel()

    with pytest.raises(AuditCancelledError):
        next(
            Auditor(StubService(events), AuditOptions(dry_run=True)).audit(
                source, token
            )
        )
    assert events == ["specs:close"]


def test_dry_run_cancelled_mid_consumption(deps):
    events = []
    token = CountingToken(cancel_on_check=2)
    gen = Auditor(StubService(events), AuditOptions(dry_run=True)).audit(
        LazySource(deps, events), token
    )

    with pytest.raises(AuditCancelledError):
        list(gen)
    assert [e for e in events if not e.endswith(":close")] == ["specs:next:pkg-a"]


def test_iterator_without_close_is_supported(deps):
    class NoCloseSource(DependencySource):
        def __init__(self, items):
            self._items = items
            self.pulled = 0

        def collect(self) -> Iterator[ResolvedDependency]:
            return self._generate()

        def _generate(self):
            for item in self._items:
                self.pulled += 1
                yield item

        def fix(self, _fix_version):
            raise NotImplementedError

    source = NoCloseSource(deps)
    results = list(Auditor(StubService([])).audit(source))
    assert [d.name for d, _ in results] == ["pkg-a", "pkg-b", "pkg-c"]
    assert source.pulled == 3


def test_dedup_algorithm_unchanged():
    vulns = [
        VulnerabilityResult(
            id="PYSEC-0",
            description="fake",
            fix_versions=[Version("1.1.0")],
            aliases={"CVE-X"},
        ),
        VulnerabilityResult(
            id="CVE-X",
            description="fake",
            fix_versions=[Version("1.1.0")],
            aliases={"OTHER-1"},
        ),
    ]

    class DedupService(VulnerabilityService):
        def query(self, spec):
            return spec, list(vulns)

    results = list(Auditor(DedupService()).audit(LazySource([_dep("pkg-a")], [])))
    assert len(results) == 1
    assert len(results[0][1]) == 1
    assert results[0][1][0].id == "PYSEC-0"
    assert results[0][1][0].aliases == {"CVE-X", "OTHER-1"}
