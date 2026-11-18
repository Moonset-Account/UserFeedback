"""
Core auditing APIs.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

from pip_audit._dependency_source import DependencySource
from pip_audit._service import Dependency, VulnerabilityResult, VulnerabilityService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditOptions:
    """
    Settings the control the behavior of an `Auditor` instance.
    """

    dry_run: bool = False


class AuditCancelledError(Exception):
    """
    Raised by `Auditor.audit` when a `CancellationToken` signals cancellation.

    This exception is distinct from service failures: callers can distinguish
    a cooperative cancellation (`AuditCancelledError`) from an underlying
    vulnerability-service failure (`pip_audit._service.ServiceError`).
    """

    pass


class CancellationToken:
    """
    A cooperative cancellation token for an in-progress `Auditor.audit` step.

    The token starts uncancelled. Any party holding a reference to it can
    request cancellation with `cancel`; the `Auditor` polls `is_cancelled`
    at its dependency boundaries (before pulling the next dependency and
    before publishing each result). Cancellation is therefore cooperative:
    an in-progress `next` call, including one inside a custom
    `VulnerabilityService.query_all` generator, is allowed to finish, and no
    interruption of work already in flight is promised.
    """

    def __init__(self) -> None:
        self._cancelled: bool = False

    def cancel(self) -> None:
        """
        Request cancellation.

        Cancellation is sticky: once requested, later checks continue to
        report cancellation.
        """
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """
        Return whether cancellation has been requested.
        """
        return self._cancelled


def _raise_if_cancelled(token: Optional[CancellationToken]) -> None:
    if token is not None and token.is_cancelled():
        raise AuditCancelledError("audit cancelled by cancellation token")


def _safe_close(iterator: object, original_error: Optional[BaseException]) -> None:
    """
    Close an iterator that the library created, if it supports `close`.

    Exceptions raised while closing are logged and suppressed when another
    error is already in flight, so that teardown failures never replace the
    exception that the audit is actually propagating (the original service
    error or the `AuditCancelledError`). When no error is in flight, a
    teardown failure is allowed to surface.
    """
    close = getattr(iterator, "close", None)
    if not callable(close):
        return

    try:
        close()
    except Exception as close_error:
        if original_error is not None:
            logger.warning(
                "suppressing exception while closing iterator during error handling: "
                f"{close_error}"
            )
        else:
            raise


class Auditor:
    """
    The core class of the `pip-audit` API.

    For a given dependency source and vulnerability service, supply a mapping of dependencies to
    known vulnerabilities.
    """

    def __init__(
        self,
        service: VulnerabilityService,
        options: AuditOptions = AuditOptions(),
    ):
        """
        Create a new auditor. Auditors start with no dependencies to audit;
        each `audit` step is fed a `DependencySource`.

        The behavior of the auditor can be optionally tweaked with the `options`
        parameter.
        """
        self._service = service
        self._options = options

    def audit(
        self,
        source: DependencySource,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Iterator[tuple[Dependency, list[VulnerabilityResult]]]:
        """
        Perform the auditing step, collecting dependencies from `source`.

        Individual vulnerability results are uniqued based on their `aliases` sets:
        any two results for the same dependency that share an alias are collapsed
        into a single result with a union of all aliases.

        `PYSEC`-identified results are given priority over other results.

        If `cancellation_token` is supplied and becomes cancelled, the audit
        stops cooperatively at the next dependency boundary: the token is
        checked before the next dependency is pulled from the source and
        before each result is published to the caller. Already-published
        results are retained; after cancellation no further dependency is
        pulled and no not-yet-delivered result is published. Cancelling does
        not forcibly interrupt a `next` call already in flight (including
        work inside a custom `VulnerabilityService.query_all` generator):
        the in-flight call is allowed to complete. Cancellation raises
        `AuditCancelledError`, which is distinct from the service's own
        `ServiceError`.

        When `cancellation_token` is `None` (the default), the behavior is
        unchanged from the uncancellable interface.

        On cancellation, service failure, or early closure of this generator
        by the caller, the internally-held iterators (the dependency source
        iterator and the `query_all` result iterator) that support `close`
        are closed. Exceptions raised during this teardown do not overwrite
        the exception already being propagated.
        """
        specs = source.collect()
        if self._options.dry_run:
            return self._audit_dry_run(specs, cancellation_token)
        return self._audit_live(specs, cancellation_token)

    def _audit_dry_run(
        self,
        specs: Iterator[Dependency],
        cancellation_token: Optional[CancellationToken],
    ) -> Iterator[tuple[Dependency, list[VulnerabilityResult]]]:
        package_count = 0
        try:
            while True:
                # Check before pulling the next dependency from the source.
                _raise_if_cancelled(cancellation_token)
                try:
                    next(specs)
                except StopIteration:
                    break
                package_count += 1
            logger.info(f"Dry run: would have audited {package_count} packages")
            yield from ()
        finally:
            _safe_close(specs, sys.exc_info()[0])

    def _audit_live(
        self,
        specs: Iterator[Dependency],
        cancellation_token: Optional[CancellationToken],
    ) -> Iterator[tuple[Dependency, list[VulnerabilityResult]]]:
        results = self._service.query_all(specs)
        try:
            while True:
                # Check before pulling the next dependency/result. An
                # in-flight `next` (including work inside a custom
                # `query_all` generator) is allowed to finish; the token is
                # only observed at this boundary.
                _raise_if_cancelled(cancellation_token)
                try:
                    dep, vulns = next(results)
                except StopIteration:
                    break

                unique_vulns: list[VulnerabilityResult] = []
                seen_aliases: set[str] = set()

                # First pass, add all PYSEC vulnerabilities and track their
                # alias sets.
                for v in vulns:
                    if not v.id.startswith("PYSEC"):
                        continue

                    seen_aliases.update(v.aliases | {v.id})
                    unique_vulns.append(v)

                # Second pass: add any non-PYSEC vulnerabilities.
                for v in vulns:
                    # If we've already seen this vulnerability by another name,
                    # don't add it. Instead, find the previous result and update
                    # its alias set.
                    if seen_aliases.intersection(v.aliases | {v.id}):
                        idx, previous = next(
                            (i, p) for (i, p) in enumerate(unique_vulns) if p.alias_of(v)
                        )
                        unique_vulns[idx] = previous.merge_aliases(v)
                        continue

                    seen_aliases.update(v.aliases | {v.id})
                    unique_vulns.append(v)

                # Check before publishing this result. If cancelled, the
                # not-yet-delivered result is dropped and the source is not
                # advanced any further.
                _raise_if_cancelled(cancellation_token)
                yield dep, unique_vulns
        finally:
            # Teardown in reverse acquisition order: the service result
            # iterator first, then the dependency source iterator. Suppress
            # teardown errors so they cannot mask the in-flight exception.
            original_error = sys.exc_info()[0]
            _safe_close(results, original_error)
            _safe_close(specs, original_error)
