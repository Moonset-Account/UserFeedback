"""Streaming median window operator.

Semantics
---------
Input records arrive partitioned and, within a partition, in non-decreasing
integer time.  Times may be negative; the window length ``W`` is a
non-negative integer.

For every record at time ``t`` the operator reports the median of the
**closed** window ``[t - W, t]`` after removing every record whose time equals
``t`` (in the same partition).  Equivalently: the median of all values with
``t - W <= time <= t - 1``.

* even number of values -> arithmetic mean of the two middle values
* odd number of values  -> the middle value itself
* empty window          -> ``None`` (SQL NULL)

Because a record at time ``t`` never sees another record at time ``t``, the
output of a record does not depend on where a batch boundary falls inside a
timestamp group.  The operator is strictly forward-reading: nothing older than
``t - W`` is retained, so resident state is bounded by the number of rows in
the active window ``[t - W, t]``.

Values live in a treap (see :mod:`median_window.treap`).  Each inserted or
evicted value touches one root-to-leaf path; the window is never re-sorted and
there are no full rebuilds.  Per-operation churn is reported by
:meth:`MedianWindow.stats`.

Crossing a partition boundary discards all state and starts fresh.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Iterator, NamedTuple, Sequence

from .treap import OrderedMultiset

__all__ = [
    "MedianWindow",
    "WindowRow",
    "WindowError",
    "OrderingError",
    "MEDIAN_NULL",
]

#: Sentinel used to document "SQL NULL" in prose; the code returns ``None``.
MEDIAN_NULL = None


class WindowError(ValueError):
    """Invalid window configuration or record."""


class OrderingError(WindowError):
    """Input violated the partition/time ordering contract."""


@dataclass(frozen=True)
class WindowRow:
    """One emitted output row, preserving input order.

    Attributes
    ----------
    row_number:
        1-based global arrival index of the corresponding input record.
    partition:
        Partition key of the record.
    time:
        Integer timestamp of the record.
    value:
        The record's numeric value.
    median:
        The reported window median, or ``None`` (SQL NULL) when empty.
    """

    row_number: int
    partition: object
    time: int
    value: object
    median: object


class WindowStats(NamedTuple):
    """Observable state and structural churn.

    ``insertions`` and ``deletions`` count single-value operations on the
    ordered structure; ``rotations`` counts treap rebalances; ``rebuilds``
    counts whole-window re-sorts and is always ``0``.  Counters accumulate
    across the whole run (all partitions).
    """

    rows_in: int
    rows_out: int
    insertions: int
    deletions: int
    rotations: int
    merge_steps: int
    probes: int
    kth_queries: int
    active_size: int
    pending_size: int
    resident_rows: int
    max_resident: int
    non_null_size: int
    partitions_seen: int
    rebuilds: int
    max_window_rows: int


def _check_window(window) -> int:
    if isinstance(window, bool) or not isinstance(window, int):
        raise WindowError("window must be an int")
    if window < 0:
        raise WindowError("window must be >= 0")
    return window


def _check_time(time) -> int:
    if isinstance(time, bool) or not isinstance(time, int):
        raise WindowError("time must be an int")
    return time


def _check_value(value):
    """A record value may be numeric or SQL NULL (``None``).

    ``None`` marks a NULL that occupies a row and produces an output row but
    never contributes to a median, mirroring the "non-null numeric values"
    wording of the specification.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WindowError("value must be an int, a float, or None")
    if isinstance(value, float) and math.isnan(value):
        raise WindowError("value must not be NaN")
    return value


def _check_partition(partition):
    if partition is None:
        raise WindowError("partition must not be None")
    try:
        hash(partition)
    except TypeError as exc:
        raise WindowError("partition must be hashable") from exc
    return partition


def _median_of(values: Sequence) -> object:
    """Reference median over the non-NULL values (``None`` if none remain)."""
    ordered = sorted(v for v in values if v is not None)
    n = len(ordered)
    if n == 0:
        return None
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


class MedianWindow:
    """Push-based median window with a bound on resident rows.

    Parameters
    ----------
    window:
        Non-negative integer ``W`` of the closed window ``[t - W, t]``.
    seed:
        Seed for the treap's pseudorandom priorities.  It only affects the
        tree's shape, never the reported medians, and makes churn counters
        reproducible across runs.
    retain_results:
        When ``True`` the operator remembers every emitted
        :class:`WindowRow` (exposed via :attr:`results`).  Default ``False``
        keeps the operator's own memory bounded to the active window; callers
        that need the full history can collect the return values of
        :meth:`push`.
    contiguous_partitions:
        Default ``False`` simply restarts on every partition change, so even a
        partition key that reappears is treated as a fresh start.  ``True``
        makes the operator strict: it remembers every closed partition key and
        rejects a reappearing one with :class:`OrderingError` (the input
        contract groups partitions into contiguous blocks).  Strict mode keeps
        one small key per partition, never rows; the default keeps only the
        active window plus a single partition key.

    Examples
    --------
    >>> w = MedianWindow(2, retain_results=True)
    >>> w.push("p", 0, 10).median is None
    True
    >>> w.push("p", 1, 2).median
    10
    >>> w.push("p", 1, 4).median
    10
    >>> w.push("p", 2, 7).median
    4
    >>> [r.row_number for r in w.results]
    [1, 2, 3, 4]
    """

    def __init__(
        self,
        window: int,
        seed: int = 0,
        retain_results: bool = False,
        contiguous_partitions: bool = False,
    ) -> None:
        self._window = _check_window(window)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise WindowError("seed must be an int")
        if not isinstance(retain_results, bool):
            raise WindowError("retain_results must be a bool")
        if not isinstance(contiguous_partitions, bool):
            raise WindowError("contiguous_partitions must be a bool")
        self._seed = seed
        self._retain_results = retain_results
        self._contiguous = contiguous_partitions
        self._tree = OrderedMultiset(seed=seed)
        self._active: deque[tuple[int, object]] = deque()
        self._pending: list[object] = []
        self._pending_time: int | None = None
        self._partition = None
        self._closed_partitions: set = set()
        self._started = False
        self._finished = False
        self._row_number = 0
        self._rows_out = 0
        self._max_resident = 0
        self._partitions_seen = 0
        self._rebuilds = 0
        self._max_window_rows = 0
        self._emitted: list[WindowRow] = []

    # ------------------------------------------------------------- properties
    @property
    def window(self) -> int:
        return self._window

    @property
    def row_number(self) -> int:
        """Number of records consumed so far."""
        return self._row_number

    @property
    def active_size(self) -> int:
        """Rows currently contributing to medians (times in ``[t-W, t-1]``)."""
        return len(self._active)

    @property
    def non_null_size(self) -> int:
        """Non-NULL values currently in the ordered structure."""
        return self._tree.size

    @property
    def pending_size(self) -> int:
        """Rows of the current timestamp group, not yet part of the window."""
        return len(self._pending)

    @property
    def results(self) -> tuple[WindowRow, ...]:
        """Rows emitted so far, in input order.

        Only populated when the operator was built with
        ``retain_results=True``; otherwise empty, so that resident memory
        stays bounded by the active window.
        """
        return tuple(self._emitted)

    # ------------------------------------------------------------------ push
    def push(self, partition, time, value) -> WindowRow:
        """Consume one record and return its output row."""
        if self._finished:
            raise WindowError("operator has been finished")
        _check_partition(partition)
        time = _check_time(time)
        value = _check_value(value)

        if not self._started or partition != self._partition:
            self._switch_partition(partition)
            self._pending_time = time

        assert self._pending_time is not None
        if time < self._pending_time:
            raise OrderingError(
                f"time moved backwards within partition {partition!r}: "
                f"{time} < {self._pending_time}"
            )
        if time > self._pending_time:
            self._advance(time)

        # Rows at ``time`` are excluded from their own output, hence the
        # median of the active window is exactly the required answer and is
        # identical for every row sharing this timestamp.
        median = self._tree.median()

        self._pending.append(value)
        self._row_number += 1
        self._rows_out += 1
        resident = len(self._active) + len(self._pending)
        if resident > self._max_resident:
            self._max_resident = resident
        row = WindowRow(self._row_number, partition, time, value, median)
        if self._retain_results:
            self._emitted.append(row)
        return row

    def push_many(self, records: Iterable) -> list[WindowRow]:
        """Consume an iterable of ``(partition, time, value)`` tuples."""
        out = []
        for record in records:
            if isinstance(record, (str, bytes)) or not isinstance(
                record, (tuple, list)
            ) or len(record) != 3:
                raise WindowError(
                    "each record must be a (partition, time, value) triple"
                )
            partition, time, value = record
            out.append(self.push(partition, time, value))
        return out

    def push_batches(self, batches: Iterable[Iterable]) -> list[WindowRow]:
        """Consume an iterable of batches, each a sequence of triples.

        Batch boundaries may fall anywhere, including inside a group of rows
        sharing the same timestamp; this must not change any output.
        """
        out = []
        for batch in batches:
            out.extend(self.push_many(batch))
        return out

    # ---------------------------------------------------------------- finish
    def finish(self) -> tuple[WindowRow, ...]:
        """Close the stream.  Idempotent.

        Every row was already emitted by :meth:`push`; the returned tuple is
        the retained history (empty unless ``retain_results=True``).
        """
        self._finished = True
        return self.results

    # --------------------------------------------------------------- helpers
    def _switch_partition(self, partition) -> None:
        """Discard every piece of per-partition state and start fresh.

        With ``contiguous_partitions=True`` (the default) a partition that
        reappears after being closed is rejected as an ordering violation.
        The bookkeeping holds only *partition keys* -- never rows -- so the
        resident row state stays bounded by the active window either way.
        """
        if self._contiguous:
            if self._started:
                self._closed_partitions.add(self._partition)
            if partition in self._closed_partitions:
                raise OrderingError(
                    f"partition {partition!r} reappeared after closing; "
                    "partitions must be contiguous blocks"
                )
        self._partition = partition
        self._started = True
        self._partitions_seen += 1
        self._tree.clear()
        self._active.clear()
        self._pending = []
        self._pending_time = None

    def _advance(self, time: int) -> None:
        """Flush the previous timestamp group, then evict rows below ``t - W``.

        Rows outside the closed window ``[t - W, t]`` are dropped without ever
        entering the ordered structure, so the structure only ever holds rows
        that are currently inside the active window.
        """
        assert self._pending_time is not None
        lower = time - self._window
        while self._active and self._active[0][0] < lower:
            _, stale = self._active.popleft()
            if stale is not None:
                self._tree.discard(stale)
        if self._pending and self._pending_time >= lower:
            stamp = self._pending_time
            for value in self._pending:
                if value is not None:
                    self._tree.add(value)
                self._active.append((stamp, value))
        self._pending = []
        self._pending_time = time
        if len(self._active) > self._max_window_rows:
            self._max_window_rows = len(self._active)

    # ----------------------------------------------------------------- stats
    def stats(self) -> WindowStats:
        """Snapshot of state and structural churn (see :class:`WindowStats`)."""
        return WindowStats(
            rows_in=self._row_number,
            rows_out=self._rows_out,
            insertions=self._tree.insertions,
            deletions=self._tree.deletions,
            rotations=self._tree.rotations,
            merge_steps=self._tree.merge_steps,
            probes=self._tree.probes,
            kth_queries=self._tree.kth_queries,
            active_size=len(self._active),
            pending_size=len(self._pending),
            resident_rows=len(self._active) + len(self._pending),
            max_resident=self._max_resident,
            non_null_size=self._tree.size,
            partitions_seen=self._partitions_seen,
            rebuilds=self._rebuilds,
            max_window_rows=self._max_window_rows,
        )


def reference_output(records: Iterable, window: int) -> list[object]:
    """Brute-force reference medians for a complete, ordered record stream.

    ``records`` is an iterable of ``(partition, time, value)`` triples in the
    contract's order.  Returns one median per record (``None`` for empty
    windows).  Used to validate the streaming operator; not on any hot path.
    """
    window = _check_window(window)
    out: list[object] = []
    grouped: list[list[tuple[int, object]]] = []
    current_partition = object()
    for index, (partition, time, value) in enumerate(records):
        _check_partition(partition)
        time = _check_time(time)
        value = _check_value(value)
        if index == 0 or partition != current_partition:
            grouped.append([])
            current_partition = partition
        grouped[-1].append((time, value))
        bucket = grouped[-1]
        out.append(
            _median_of(
                [v for (t, v) in bucket if time - window <= t <= time - 1]
            )
        )
    return out


def run_stream(
    records: Iterable, window: int, seed: int = 0
) -> list[WindowRow]:
    """Convenience: feed an ordered record stream through a new operator.

    Returns every emitted row in input order (history is retained for you).
    """
    op = MedianWindow(window, seed=seed, retain_results=True)
    op.push_many(records)
    op.finish()
    return list(op.results)


def iter_medians(records: Iterable, window: int, seed: int = 0) -> Iterator:
    """Lazily yield each output median for an ordered record stream."""
    op = MedianWindow(window, seed=seed)
    for partition, time, value in records:
        yield op.push(partition, time, value).median
