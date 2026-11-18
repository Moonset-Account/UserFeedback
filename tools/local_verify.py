#!/usr/bin/env python
"""
Local verification harness for the cooperative-cancellation audit flow.

This script is the replayable entry point for cases C01..C04. It builds only
in-memory synthetic dependency sources, vulnerability-service stubs and
controllable cancellation tokens: no network, no cloud services, no real
packages and no credentials are involved.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C01,C02
    python tools/local_verify.py            # run every case

The process exits non-zero if any assertion fails, a case raises an
unexpected exception, a case performs zero assertions, or a requested case
id is unknown. Each case prints one JSON record with its case id, the
actual number of assertions executed, passed/failed/skipped counts and a
summary of the seed/expected/observed values.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Optional

# Make the workspace importable when the script is run directly, including
# under `python -I` (which omits the script directory's sys.path handling).
_WORKSPACE = Path(__file__).resolve().parent.parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from packaging.version import Version  # noqa: E402

from pip_audit._audit import (  # noqa: E402
    AuditCancelledError,
    AuditOptions,
    Auditor,
    CancellationToken,
)
from pip_audit._dependency_source.interface import DependencySource  # noqa: E402
from pip_audit._service.interface import (  # noqa: E402
    ResolvedDependency,
    ServiceError,
    VulnerabilityResult,
    VulnerabilityService,
)


# ---------------------------------------------------------------------------
# Assertion helper: every check is counted so a case with zero real
# assertions cannot masquerade as a pass.
# ---------------------------------------------------------------------------


class CheckRecorder:
    def __init__(self) -> None:
        self.assertions = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        self.assertions += 1
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(message)

    def check_equal(self, actual: object, expected: object, message: str) -> None:
        self.check(
            actual == expected,
            f"{message} (expected={expected!r}, observed={actual!r})",
        )

    def check_raises(
        self,
        expected_exc: type[BaseException],
        callable_: Callable[[], object],
        message: str,
    ) -> Optional[BaseException]:
        self.assertions += 1
        try:
            callable_()
        except expected_exc as exc:
            self.passed += 1
            return exc
        except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed
            self.failed += 1
            self.failures.append(
                f"{message} (expected {expected_exc.__name__}, "
                f"observed {type(exc).__name__}: {exc})"
            )
            return None
        else:
            self.failed += 1
            self.failures.append(
                f"{message} (expected {expected_exc.__name__}, observed no exception)"
            )
            return None


# ---------------------------------------------------------------------------
# Synthetic, access-recording lazy dependency source.
# ---------------------------------------------------------------------------


def _make_dep(name: str, version: str = "1.0.0") -> ResolvedDependency:
    return ResolvedDependency(name=name, version=Version(version))


class RecordingIterator:
    """
    Iterator wrapper that records every access and records its own close.

    Optionally raises `close_error` when closed, so teardown-order and
    exception-preservation behavior can be observed.
    """

    def __init__(
        self,
        items: list[ResolvedDependency],
        events: list[str],
        label: str,
        close_error: Optional[Exception] = None,
    ) -> None:
        self._items = list(items)
        self._index = 0
        self._events = events
        self._label = label
        self._close_error = close_error
        self.closed = False

    def __iter__(self) -> RecordingIterator:
        return self

    def __next__(self) -> ResolvedDependency:
        if self._index >= len(self._items):
            self._events.append(f"{self._label}:exhausted")
            raise StopIteration
        item = self._items[self._index]
        self._index += 1
        self._events.append(f"{self._label}:next:{item.name}")
        return item

    def close(self) -> None:
        self.closed = True
        self._events.append(f"{self._label}:close")
        if self._close_error is not None:
            raise self._close_error


class LazyRecordingSource(DependencySource):
    """
    A dependency source whose `collect` returns a fresh lazy iterator.

    Nothing is pulled until the auditor advances the iterator, which lets us
    prove that cancellation before consumption never touches the source.
    """

    def __init__(
        self,
        deps: list[ResolvedDependency],
        events: list[str],
        close_error: Optional[Exception] = None,
    ) -> None:
        self._deps = deps
        self._events = events
        self._close_error = close_error
        self.iterator: Optional[RecordingIterator] = None

    def collect(self) -> Iterator[ResolvedDependency]:
        self.iterator = RecordingIterator(
            self._deps, self._events, "specs", self._close_error
        )
        return self.iterator

    def fix(self, _fix_version: object) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Vulnerability service stubs.
# ---------------------------------------------------------------------------


class StubService(VulnerabilityService):
    """
    Default per-dependency service. Each `query` emits a traceable result.
    """

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def query(self, spec: ResolvedDependency):
        self._events.append(f"service:query:{spec.name}")
        vuln = VulnerabilityResult(
            id=f"VULN-{spec.name.upper()}",
            description=f"synthetic result for {spec.name}",
            fix_versions=[Version("2.0.0")],
            aliases=set(),
        )
        return spec, [vuln]


class BatchService(VulnerabilityService):
    """
    Service with a custom generator-based `query_all` override.

    The generator records each pull, and can raise from `close` so that
    teardown exception suppression is observable.
    """

    def __init__(self, events: list[str], close_error: Optional[Exception] = None) -> None:
        self._events = events
        self._close_error = close_error
        self.generator_closed = False
        self._generator: Optional[Iterator[object]] = None

    def query(self, spec: ResolvedDependency):  # pragma: no cover - unused
        raise NotImplementedError

    def query_all(self, specs: Iterator[ResolvedDependency]):
        self._generator = self._generate(specs)
        return self._generator

    def _generate(self, specs: Iterator[ResolvedDependency]):
        try:
            for spec in specs:
                self._events.append(f"service:query_all:{spec.name}")
                yield spec, [
                    VulnerabilityResult(
                        id=f"BATCH-{spec.name.upper()}",
                        description="synthetic batched result",
                        fix_versions=[Version("9.9.9")],
                        aliases=set(),
                    )
                ]
        finally:
            self.generator_closed = True
            self._events.append("service:query_all:close")
            if self._close_error is not None:
                raise self._close_error


class DedupService(VulnerabilityService):
    """
    Service returning alias-overlapping results for every dependency.
    """

    def __init__(self, vulns: list[VulnerabilityResult]) -> None:
        self._vulns = vulns

    def query(self, spec: ResolvedDependency):
        return spec, list(self._vulns)


# ---------------------------------------------------------------------------
# Controllable cancellation token.
# ---------------------------------------------------------------------------


class CountingCancellationToken(CancellationToken):
    """
    Token that records every poll and can cancel on the Nth poll or via a
    callback fired from inside an in-flight `next`.
    """

    def __init__(
        self,
        cancel_on_check: Optional[int] = None,
        during_next_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.checks = 0
        self._cancel_on_check = cancel_on_check
        self._during_next_callback = during_next_callback

    def is_cancelled(self) -> bool:
        self.checks += 1
        if self._cancel_on_check is not None and self.checks >= self._cancel_on_check:
            self.cancel()
        return super().is_cancelled()

    def fire_during_next(self) -> None:
        if self._during_next_callback is not None:
            self._during_next_callback()


class CallbackBatchService(BatchService):
    """
    Batch service that invokes a callback while a `next` is in flight,
    allowing the token to be cancelled mid-`next` deterministically.
    """

    def __init__(self, events: list[str], token: CountingCancellationToken) -> None:
        super().__init__(events)
        self._token = token

    def _generate(self, specs: Iterator[ResolvedDependency]):
        try:
            for spec in specs:
                # Simulate work happening inside the in-flight next() call:
                # cancellation requested while the generator is suspended
                # here must not forcibly interrupt this next().
                self._token.fire_during_next()
                self._events.append(f"service:callback_batch:{spec.name}")
                yield spec, [
                    VulnerabilityResult(
                        id=f"CB-{spec.name.upper()}",
                        description="synthetic callback result",
                        fix_versions=[Version("3.1.4")],
                        aliases=set(),
                    )
                ]
        finally:
            self.generator_closed = True
            self._events.append("service:callback_batch:close")


# ---------------------------------------------------------------------------
# Case implementations.
# ---------------------------------------------------------------------------


def case_c01(rec: CheckRecorder) -> dict:
    """
    Default (no-token) behavior: lazy access order, published order and
    results match the uncancellable interface; dedup is untouched.
    """
    events: list[str] = []
    deps = [_make_dep("pkg-a"), _make_dep("pkg-b"), _make_dep("pkg-c")]
    source = LazyRecordingSource(deps, events)
    service = StubService(events)
    auditor = Auditor(service)

    gen = auditor.audit(source)
    rec.check(isinstance(gen, Iterator), "audit() without token still returns an iterator")

    first = next(gen)
    rec.check_equal(first[0].name, "pkg-a", "first published dependency order")
    rec.check_equal(
        [v.id for v in first[1]], ["VULN-PKG-A"], "first published result ids"
    )
    rec.check_equal(
        events,
        ["specs:next:pkg-a", "service:query:pkg-a"],
        "source is pulled lazily and only as far as consumed",
    )

    rest = list(gen)
    rec.check_equal([d.name for d, _ in rest], ["pkg-b", "pkg-c"], "remaining order")
    rec.check_equal(len(rest[0][1]), 1, "each published dependency carries its result")
    rec.check_equal(
        events,
        [
            "specs:next:pkg-a",
            "service:query:pkg-a",
            "specs:next:pkg-b",
            "service:query:pkg-b",
            "specs:next:pkg-c",
            "service:query:pkg-c",
            "specs:exhausted",
            "specs:close",
        ],
        "full consumption pulls every dependency exactly once, in order, then closes",
    )
    rec.check(
        source.iterator is not None and source.iterator.closed,
        "source iterator closed after exhaustion",
    )

    # Passing an explicit but never-cancelled token changes nothing.
    events2: list[str] = []
    source2 = LazyRecordingSource(deps, events2)
    service2 = StubService(events2)
    results2 = list(Auditor(service2).audit(source2, CancellationToken()))
    rec.check_equal([d.name for d, _ in results2], ["pkg-a", "pkg-b", "pkg-c"],
                    "uncancelled token preserves default order")

    seed = {"dependencies": [d.name for d in deps], "token": None}
    return {"seed": seed, "events": events, "published": [d.name for d, _ in [first] + rest]}


def case_c02(rec: CheckRecorder) -> dict:
    """
    Cancellation boundaries: before consumption, inside an in-flight next,
    and before publishing a not-yet-delivered result.
    """
    deps = [_make_dep("pkg-a"), _make_dep("pkg-b"), _make_dep("pkg-c")]
    observed: dict[str, object] = {}

    # (A) Already cancelled before the first next(): source never advanced.
    events_a: list[str] = []
    source_a = LazyRecordingSource(deps, events_a)
    service_a = StubService(events_a)
    token_a = CancellationToken()
    token_a.cancel()
    gen_a = Auditor(service_a).audit(source_a, token_a)
    rec.check_raises(AuditCancelledError, lambda: next(gen_a), "pre-cancelled audit raises AuditCancelledError")
    rec.check_equal(
        [e for e in events_a if not e.endswith(":close")],
        [],
        "pre-cancelled audit never pulls from the source (only teardown close is recorded)",
    )
    rec.check_equal(events_a, ["specs:close"], "pre-cancel still closes the source iterator")
    rec.check(source_a.iterator is not None and source_a.iterator.closed, "pre-cancel closes source iterator")
    rec.check_raises(StopIteration, lambda: next(gen_a), "cancelled generator does not resume")

    # (B) Cancel during an in-flight next(): the next completes, the result
    # is withheld because the pre-publish check observes the token.
    events_b: list[str] = []
    source_b = LazyRecordingSource(deps, events_b)
    token_b = CountingCancellationToken()
    # Cancellation is requested from inside the custom query_all generator,
    # while the auditor's next() call is suspended in it.
    token_b._during_next_callback = token_b.cancel  # type: ignore[method-assign]
    service_b = CallbackBatchService(events_b, token_b)
    gen_b = Auditor(service_b).audit(source_b, token_b)
    rec.check_raises(AuditCancelledError, lambda: next(gen_b),
                     "mid-next cancellation surfaces at the publish boundary")
    rec.check_equal(
        events_b,
        ["specs:next:pkg-a", "service:callback_batch:pkg-a", "service:callback_batch:close", "specs:close"],
        "in-flight next completes; no second dependency is pulled",
    )
    rec.check(service_b.generator_closed, "custom query_all generator closed after cancellation")
    rec.check(source_b.iterator is not None and source_b.iterator.closed, "source closed after mid-next cancellation")
    observed["mid_next_events"] = list(events_b)

    # (C) Cancel after one published result, before the next dependency is
    # pulled: published result retained, no further advancement.
    events_c: list[str] = []
    source_c = LazyRecordingSource(deps, events_c)
    token_c = CountingCancellationToken(cancel_on_check=3)
    service_c = StubService(events_c)
    gen_c = Auditor(service_c).audit(source_c, token_c)
    first_c = next(gen_c)
    rec.check_equal(first_c[0].name, "pkg-a", "first result is published before cancellation")
    rec.check_raises(AuditCancelledError, lambda: next(gen_c),
                     "cancellation before next pull raises AuditCancelledError")
    rec.check_equal(
        [e for e in events_c if not e.endswith(":close")],
        ["specs:next:pkg-a", "service:query:pkg-a"],
        "no dependency beyond the published one is pulled (teardown close excluded)",
    )
    rec.check_raises(StopIteration, lambda: next(gen_c), "cancelled generator stays stopped")
    observed["post_publish_events"] = list(events_c)
    observed["token_polls"] = token_c.checks

    # AuditCancelledError must be distinguishable from service errors.
    rec.check(not issubclass(AuditCancelledError, ServiceError),
              "AuditCancelledError is distinct from ServiceError")

    return {"seed": {"dependencies": [d.name for d in deps]}, "observed": observed}


def case_c03(rec: CheckRecorder) -> dict:
    """
    Internal iterator close ordering and exception preservation on
    cancellation, early caller close and service failure.
    """
    deps = [_make_dep("pkg-a"), _make_dep("pkg-b"), _make_dep("pkg-c")]
    observed: dict[str, object] = {}

    # (A) Caller closes the outer generator early: LIFO close order and no
    # exception escapes.
    events_a: list[str] = []
    source_a = LazyRecordingSource(deps, events_a)
    service_a = BatchService(events_a)
    gen_a = Auditor(service_a).audit(source_a)
    next(gen_a)
    gen_a.close()
    rec.check_equal(
        events_a,
        [
            "specs:next:pkg-a",
            "service:query_all:pkg-a",
            "service:query_all:close",
            "specs:close",
        ],
        "early close closes service iterator before source iterator (LIFO)",
    )
    rec.check(service_a.generator_closed and source_a.iterator is not None
              and source_a.iterator.closed, "both internal iterators closed on early close")

    # (B) Both teardowns fail during an early caller close. Generator close
    # carries an in-flight GeneratorExit, so teardown errors are suppressed
    # (never replace the close) and logged instead, in LIFO order.
    events_b: list[str] = []
    source_b = LazyRecordingSource(deps, events_b, close_error=RuntimeError("specs-close-boom"))
    service_b = BatchService(events_b, close_error=RuntimeError("service-close-boom"))
    gen_b = Auditor(service_b).audit(source_b)
    next(gen_b)
    with _capture_warnings() as cap_b:
        try:
            gen_b.close()
            close_raised = False
        except BaseException as exc:  # noqa: BLE001 - recorded as a failed assertion
            close_raised = True
            close_exc = exc
    rec.assertions += 1
    if not close_raised:
        rec.passed += 1
    else:
        rec.failed += 1
        rec.failures.append(
            f"failing teardown during generator close must not raise (observed "
            f"{type(close_exc).__name__}: {close_exc})"
        )
    rec.check_equal(
        [e for e in events_b if e.endswith(":close")],
        ["service:query_all:close", "specs:close"],
        "LIFO order retained even when closes fail",
    )
    suppressed_b = [
        r.getMessage() for r in cap_b.records
        if "suppressing exception while closing iterator" in r.getMessage()
    ]
    rec.check(
        [m.split(": ", 1)[-1] for m in suppressed_b] == ["service-close-boom", "specs-close-boom"],
        f"both early-close teardown errors suppressed in LIFO order (got {suppressed_b})",
    )

    # (C) Original service exception must not be overwritten by close errors.
    events_c: list[str] = []
    original = ServiceError("upstream outage")
    source_c = LazyRecordingSource(deps, events_c, close_error=RuntimeError("specs-close-boom"))

    class FailingBatchService(VulnerabilityService):
        """Fails mid-stream; its result iterator also fails on close."""

        def query(self, spec):  # pragma: no cover - unused
            raise NotImplementedError

        def query_all(self, specs):
            inner = self._generate(specs)

            class _FailingCloseIterator:
                def __iter__(self):
                    return self

                def __next__(self):
                    return next(inner)

                def close(self):
                    events_c.append("service:failing:close")
                    raise RuntimeError("service-close-boom")

            return _FailingCloseIterator()

        def _generate(self, specs):
            for index, spec in enumerate(specs):
                events_c.append(f"service:failing:{spec.name}")
                if index == 1:
                    raise original
                yield spec, []

    service_c = FailingBatchService()
    gen_c = Auditor(service_c).audit(source_c)
    with _capture_warnings() as cap:
        exc_c = rec.check_raises(ServiceError, lambda: list(gen_c), "original service error propagates")
    if exc_c is not None:
        rec.check_equal(str(exc_c), "upstream outage", "service error text preserved despite teardown failures")
    rec.check_equal(
        [e for e in events_c if e.endswith(":close")],
        ["service:failing:close", "specs:close"],
        "service iterator closed before source iterator on service failure",
    )
    suppressed = [r.getMessage() for r in cap.records if "suppressing exception while closing iterator" in r.getMessage()]
    rec.check(len(suppressed) == 2, f"both teardown exceptions suppressed and logged (got {suppressed})")
    observed["suppressed_logs"] = suppressed

    # (D) Cancellation with failing closes: AuditCancelledError wins.
    events_d: list[str] = []
    source_d = LazyRecordingSource(deps, events_d, close_error=RuntimeError("specs-close-boom"))
    service_d = BatchService(events_d, close_error=RuntimeError("service-close-boom"))
    token_d = CancellationToken()
    token_d.cancel()
    gen_d = Auditor(service_d).audit(source_d, token_d)
    with _capture_warnings():
        exc_d = rec.check_raises(AuditCancelledError, lambda: next(gen_d),
                                 "cancellation error beats teardown errors")
    rec.check(exc_d is not None, "AuditCancelledError instance returned for inspection")

    return {"seed": {"dependencies": [d.name for d in deps]}, "observed": observed}


def case_c04(rec: CheckRecorder) -> dict:
    """
    Dry-run cancellation, custom query_all cooperative semantics and
    unchanged vulnerability de-duplication.
    """
    deps = [_make_dep("pkg-a"), _make_dep("pkg-b"), _make_dep("pkg-c")]
    observed: dict[str, object] = {}

    # (A) Dry run without token drains the lazy source entirely.
    events_a: list[str] = []
    source_a = LazyRecordingSource(deps, events_a)
    service_a = StubService(events_a)
    with _capture_warnings() as cap:
        results_a = list(Auditor(service_a, AuditOptions(dry_run=True)).audit(source_a))
    rec.check_equal(results_a, [], "dry run publishes no results")
    rec.check_equal(
        [e for e in events_a if not e.endswith(":close")],
        ["specs:next:pkg-a", "specs:next:pkg-b", "specs:next:pkg-c", "specs:exhausted"],
        "dry run drains the source lazily",
    )
    rec.check_equal(events_a[-1], "specs:close", "dry run closes the source iterator after draining")
    dry_logs = [r.getMessage() for r in cap.records if "Dry run" in r.getMessage()]
    rec.check(len(dry_logs) == 1 and "3 packages" in dry_logs[0], f"dry-run log reports 3 packages (got {dry_logs})")
    rec.check(source_a.iterator is not None and source_a.iterator.closed, "dry-run closes source iterator")

    # (B) Dry run with pre-cancellation: nothing is pulled.
    events_b: list[str] = []
    source_b = LazyRecordingSource(deps, events_b)
    token_b = CancellationToken()
    token_b.cancel()
    gen_b = Auditor(StubService(events_b), AuditOptions(dry_run=True)).audit(source_b, token_b)
    rec.check_raises(AuditCancelledError, lambda: next(gen_b), "dry-run honors pre-cancellation")
    rec.check_equal(
        [e for e in events_b if not e.endswith(":close")],
        [],
        "cancelled dry run never advances the source (teardown close excluded)",
    )
    rec.check_equal(events_b, ["specs:close"], "cancelled dry run still closes source iterator")
    rec.check(source_b.iterator is not None and source_b.iterator.closed, "cancelled dry run closes source iterator")

    # (C) Dry run cancelled mid-consumption: already counted packages stay
    # consumed, remaining packages are never pulled.
    events_c: list[str] = []
    source_c = LazyRecordingSource(deps, events_c)
    token_c = CountingCancellationToken(cancel_on_check=2)
    gen_c = Auditor(StubService(events_c), AuditOptions(dry_run=True)).audit(source_c, token_c)
    rec.check_raises(AuditCancelledError, lambda: list(gen_c), "dry-run stops during consumption")
    rec.check_equal(
        [e for e in events_c if not e.endswith(":close")],
        ["specs:next:pkg-a"],
        "dry-run cancellation stops before pulling the next dependency",
    )
    rec.check(events_c[-1] == "specs:close", "cancelled dry run closes source iterator")

    # (D) Custom query_all generator: in-flight next() is not forcibly
    # interrupted; cancellation takes effect at the next boundary and the
    # generator is closed.
    events_d: list[str] = []
    source_d = LazyRecordingSource(deps, events_d)
    token_d = CountingCancellationToken()
    token_d._during_next_callback = token_d.cancel  # type: ignore[method-assign]
    service_d = CallbackBatchService(events_d, token_d)
    gen_d = Auditor(service_d).audit(source_d, token_d)
    rec.check_raises(AuditCancelledError, lambda: next(gen_d),
                     "custom query_all cancellation surfaces after in-flight next completes")
    rec.check_equal(
        events_d,
        ["specs:next:pkg-a", "service:callback_batch:pkg-a", "service:callback_batch:close", "specs:close"],
        "in-flight custom query_all work completes; close is performed",
    )
    rec.check(service_d.generator_closed, "custom query_all generator closed")

    # (E) Source iterator without close(): auditor must not fail teardown.
    class NoCloseSource(DependencySource):
        def __init__(self, items):
            self._items = items
            self.pulled = 0

        def collect(self):
            return self._generate()

        def _generate(self):
            for item in self._items:
                self.pulled += 1
                yield item

        def fix(self, _fix_version):
            raise NotImplementedError

    no_close_source = NoCloseSource(deps)
    results_e = list(Auditor(StubService([])).audit(no_close_source))
    rec.check_equal([d.name for d, _ in results_e], ["pkg-a", "pkg-b", "pkg-c"],
                    "iterator lacking close() is supported")
    rec.check_equal(no_close_source.pulled, 3, "no-close source fully consumed")

    # (F) De-duplication algorithm unchanged: overlapping aliases collapse.
    vulns = [
        VulnerabilityResult(
            id="PYSEC-0", description="fake", fix_versions=[Version("1.1.0")],
            aliases={"CVE-X"},
        ),
        VulnerabilityResult(
            id="CVE-X", description="fake", fix_versions=[Version("1.1.0")],
            aliases={"OTHER-1"},
        ),
    ]
    dedup_source = LazyRecordingSource([_make_dep("pkg-a")], [])
    results_f = list(Auditor(DedupService(vulns)).audit(dedup_source))
    rec.check_equal(len(results_f), 1, "one dependency published")
    rec.check_equal(len(results_f[0][1]), 1, "alias-overlapping vulns collapse to one result")
    rec.check_equal(results_f[0][1][0].id, "PYSEC-0", "PYSEC result keeps priority")
    rec.check_equal(results_f[0][1][0].aliases, {"CVE-X", "OTHER-1"}, "alias union preserved")
    observed["dedup_aliases"] = sorted(results_f[0][1][0].aliases)

    return {"seed": {"dependencies": [d.name for d in deps]}, "observed": observed}


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _WarningCapture:
    def __init__(self) -> None:
        self.handler = _ListHandler()

    @property
    def records(self) -> list[logging.LogRecord]:
        return self.handler.records

    def __enter__(self) -> "_WarningCapture":
        logger = logging.getLogger("pip_audit._audit")
        logger.addHandler(self.handler)
        logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        logging.getLogger("pip_audit._audit").removeHandler(self.handler)


def _capture_warnings() -> _WarningCapture:
    return _WarningCapture()


CASES: dict[str, Callable[[CheckRecorder], dict]] = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}

CASE_DESCRIPTIONS = {
    "C01": "Default no-token lazy ordering, published results and iterator close",
    "C02": "Cancellation at pre-consume, in-flight-next and pre-publish boundaries",
    "C03": "Internal iterator LIFO close order and original-exception preservation",
    "C04": "Dry-run cancellation, custom query_all semantics and unchanged dedup",
}


def run_case(case_id: str) -> dict:
    recorder = CheckRecorder()
    summary: dict[str, object]
    unexpected: Optional[str] = None
    try:
        detail = CASES[case_id](recorder)
        summary = detail
    except Exception as exc:  # noqa: BLE001 - any escape is a harness/case failure
        unexpected = f"{type(exc).__name__}: {exc}"
        summary = {}

    record = {
        "case": case_id,
        "description": CASE_DESCRIPTIONS[case_id],
        "assertions": recorder.assertions,
        "passed": recorder.passed,
        "failed": recorder.failed,
        "skipped": recorder.skipped,
        "failures": recorder.failures,
        "unexpected_error": unexpected,
        "summary": summary,
    }
    # A case is green only when it actually ran assertions and all passed.
    record["ok"] = (
        unexpected is None
        and recorder.assertions > 0
        and recorder.failed == 0
        and recorder.skipped == 0
    )
    return record


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local verification for cancellable pip-audit flow")
    parser.add_argument("--list", action="store_true", help="list available verification cases")
    parser.add_argument(
        "--case",
        default=None,
        help="comma-separated case ids, e.g. C01,C02; defaults to all cases",
    )
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            [{"case": cid, "description": desc} for cid, desc in CASE_DESCRIPTIONS.items()],
            indent=2,
        ))
        return 0

    if args.case:
        requested = [part.strip() for part in args.case.split(",") if part.strip()]
        unknown = [cid for cid in requested if cid not in CASES]
        if unknown:
            print(json.dumps({"error": f"unknown case id(s): {unknown}",
                              "known": sorted(CASES)}, indent=2))
            return 2
    else:
        requested = sorted(CASES)

    records = [run_case(cid) for cid in requested]
    print(json.dumps(records, indent=2, default=str))

    if not records:
        return 2
    if any(not record["ok"] for record in records):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
