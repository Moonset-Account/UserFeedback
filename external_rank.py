"""External sorting for a ranked batch window.

The operator consumes a one-shot iterable of rows, sorts them externally by
score (descending) and then streams the requested window:

    skip ``offset`` rows, emit up to ``limit`` rows, and additionally emit the
    rows that follow with the same score as the last emitted row.

Rows with an empty score are ordered after every non-empty score; empty scores
are all tied with one another and keep their input order.  Equal non-empty
scores also keep their input order (the original row ordinal is the final
tie-breaker), including across temporary segments and merge passes.

Only one forward pass over the input is made.  At most ``slots`` records are
held in memory while producing initial runs, and at most ``merge_fan_in``
records are held while merging.  ``merge_fan_in`` must be in
``[2, slots - 1]``.

Temporary segments are private files in a freshly-created temporary directory.
They are removed when the result iterator is exhausted, when :meth:`close` is
called, when a ``with`` block exits, and as a best-effort fallback when the
object is garbage-collected.

Example::

    with ExternalRanker(rows, slots=4, merge_fan_in=2, offset=10, limit=20,
                        score_key=lambda row: row["score"]) as result:
        for row in result:
            ...
"""

from __future__ import annotations

import heapq
import math
import os
import pickle
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "ExternalRanker",
    "SortStats",
    "default_score_key",
    "default_is_empty_score",
]


def default_score_key(row: Any) -> Any:
    """Return ``row["score"]`` for mappings or ``row.score`` for objects.

    A missing score is returned as ``None`` and is therefore treated as an
    empty score.
    """

    if isinstance(row, Mapping):
        return row.get("score")
    return getattr(row, "score", None)


def default_is_empty_score(value: Any) -> bool:
    """An empty score is ``None``, a floating-point NaN, or a blank string."""

    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value == ""


@dataclass
class SortStats:
    """Diagnostics for one external sort.

    ``merge_passes`` includes the final streaming merge pass.  It is ``1`` when
    the initial runs fit within the merge fan-in and ``0`` for an empty input.
    ``initial_run_sizes`` is useful for confirming that a trailing tie group
    really was spread over several temporary segments.
    """

    initial_run_sizes: list[int] = field(default_factory=list)
    merge_passes: int = 0
    segments_written: int = 0
    peak_buffered_records: int = 0
    temp_dir: Optional[str] = None

    @property
    def initial_run_count(self) -> int:
        return len(self.initial_run_sizes)


class _Record:
    """A row plus the metadata needed for a stable external ordering."""

    __slots__ = ("ordinal", "is_empty", "score", "row")

    def __init__(self, ordinal: int, is_empty: bool, score: Any, row: Any) -> None:
        self.ordinal = ordinal
        self.is_empty = is_empty
        self.score = score
        self.row = row

    def __lt__(self, other: "_Record") -> bool:
        # Non-empty scores first, in descending order.  The original input
        # ordinal makes the ordering strict and stable across merges.
        #
        # Non-empty scores must be mutually comparable.  Nothing is coerced:
        # a caller with mixed score types should normalise them in its
        # score_key so that ordering stays total and transitive.
        if self.is_empty != other.is_empty:
            return self.is_empty < other.is_empty
        if not self.is_empty and self.score != other.score:
            return self.score > other.score
        return self.ordinal < other.ordinal

    def __reduce__(self):  # keep the internal record picklable
        return (_Record, (self.ordinal, self.is_empty, self.score, self.row))


@dataclass(frozen=True)
class _Segment:
    path: str
    count: int


class _PickleCodec:
    """Default internal segment codec.

    Segment files are created by this process in a private directory and are
    only read back by this process; callers must not point the operator at
    untrusted segment files.
    """

    __slots__ = ()

    def dump(self, stream: Any, record: _Record) -> None:
        pickle.dump(record, stream, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, stream: Any) -> _Record:
        return pickle.load(stream)


def _validate_int(
    name: str,
    value: Any,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")
    return value


class ExternalRanker(Iterator[Any]):
    """Stream a stable score-ranked window from a one-shot row iterable.

    Parameters
    ----------
    rows:
        Any one-shot iterable of rows.  It is iterated exactly once.
    slots:
        Number of in-memory record slots.  Must be at least 4.
    merge_fan_in:
        Maximum number of input runs fed to one merge.  Must be between 2 and
        ``slots - 1`` inclusive, so the merge never needs more open records
        than there are record slots.
    offset:
        Number of leading ranked rows to skip.  Must be non-negative.
    limit:
        Number of rows to emit after the offset.  Must be positive.  Rows
        after that with the same score as the last emitted row are emitted in
        addition.
    score_key:
        Callable returning a row's score.  ``None`` means use
        :func:`default_score_key`.
    is_empty_score:
        Callable deciding whether a raw score is empty.  ``None`` means use
        :func:`default_is_empty_score`.
    temp_parent:
        Optional directory in which the private temporary directory is made.
        It is created if it does not exist.  ``None`` uses the system temp
        directory.
    stats:
        Optional :class:`SortStats` instance to populate.

    The returned object is both an iterator and a context manager.  Call
    :meth:`close` (or exit the ``with`` block) if iteration stops early.
    """

    def __init__(
        self,
        rows: Any,
        *,
        slots: int,
        merge_fan_in: int,
        offset: int = 0,
        limit: int,
        score_key: Optional[Callable[[Any], Any]] = None,
        is_empty_score: Optional[Callable[[Any], bool]] = None,
        temp_parent: Optional[os.PathLike[str] | str] = None,
        stats: Optional[SortStats] = None,
    ) -> None:
        # Attributes needed by __del__ are established first.
        self._closed = False
        self._gen: Optional[Iterator[Any]] = None
        self._temp_dir: Optional[str] = None
        self._segment_seq = 0

        self._slots = _validate_int("slots", slots, 4)
        self._fan_in = _validate_int(
            "merge_fan_in", merge_fan_in, 2, self._slots - 1
        )
        self._offset = _validate_int("offset", offset, 0)
        self._limit = _validate_int("limit", limit, 1)

        if score_key is None:
            score_key = default_score_key
        elif not callable(score_key):
            raise TypeError("score_key must be callable or None")
        self._score_key = score_key

        if is_empty_score is None:
            is_empty_score = default_is_empty_score
        elif not callable(is_empty_score):
            raise TypeError("is_empty_score must be callable or None")
        self._is_empty_score = is_empty_score

        try:
            self._rows = iter(rows)
        except TypeError as exc:
            raise TypeError("rows must be a one-pass iterable") from exc

        if temp_parent is not None:
            temp_parent = os.fspath(temp_parent)
        self._temp_parent = temp_parent

        self.stats = stats if stats is not None else SortStats()
        self._codec = _PickleCodec()

    # ------------------------------------------------------------------
    # Iterator / resource management
    # ------------------------------------------------------------------
    def __iter__(self) -> "ExternalRanker":
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        if self._gen is None:
            self._gen = self._run()
        return next(self._gen)

    def close(self) -> None:
        """Stop iteration and remove every temporary segment."""

        if self._closed:
            return
        self._closed = True
        generator = self._gen
        self._gen = None
        if generator is not None:
            try:
                generator.close()
            except Exception:
                # Cleanup below is still mandatory; do not let a failing
                # generator close mask leaked temporary files.
                pass
        self._cleanup()

    def __enter__(self) -> "ExternalRanker":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # External sort pipeline
    # ------------------------------------------------------------------
    def _run(self) -> Iterator[Any]:
        try:
            self._ensure_temp_dir()
            runs = self._generate_initial_runs()
            if not runs:
                self.stats.merge_passes = 0
                return

            while len(runs) > self._fan_in:
                self.stats.merge_passes += 1
                runs = self._merge_pass(runs)

            # The final pass streams directly to the consumer instead of
            # writing another segment.
            self.stats.merge_passes += 1
            yield from self._emit_window(runs)
        finally:
            self._cleanup()

    def _generate_initial_runs(self) -> list[_Segment]:
        runs: list[_Segment] = []
        buffer: list[_Record] = []
        ordinal = 0

        # next() is used directly: the caller's iterable is entered exactly
        # once and is never inspected for length or reused.
        while True:
            try:
                row = next(self._rows)
            except StopIteration:
                break

            raw_score = self._score_key(row)
            is_empty = bool(self._is_empty_score(raw_score))
            score = None if is_empty else raw_score
            buffer.append(_Record(ordinal, is_empty, score, row))
            ordinal += 1

            if len(buffer) >= self._slots:
                self._spill(buffer, runs)
                buffer = []

        if buffer:
            self._spill(buffer, runs)
        return runs

    def _spill(self, records: list[_Record], runs: list[_Segment]) -> None:
        self._note_peak(len(records))
        records.sort()
        path = self._new_segment_path()
        count = self._write_segment(path, records)
        self.stats.initial_run_sizes.append(count)
        self.stats.segments_written += 1
        runs.append(_Segment(path, count))

    def _merge_pass(self, runs: list[_Segment]) -> list[_Segment]:
        merged: list[_Segment] = []
        for start in range(0, len(runs), self._fan_in):
            group = runs[start : start + self._fan_in]
            if len(group) == 1:
                # Carry a lone run forward rather than rewriting it.
                merged.append(group[0])
                continue

            path = self._new_segment_path()
            count = self._merge_to_segment(group, path)
            self.stats.segments_written += 1
            merged.append(_Segment(path, count))
            for segment in group:
                self._unlink(segment.path)
        return merged

    def _merge_to_segment(
        self, group: list[_Segment], output_path: str
    ) -> int:
        iterators = [self._iter_segment(segment.path) for segment in group]
        count = 0
        try:
            with open(output_path, "wb") as output:
                for record in self._merged(iterators):
                    self._codec.dump(output, record)
                    count += 1
        finally:
            for iterator in iterators:
                iterator.close()
        return count

    def _emit_window(self, runs: list[_Segment]) -> Iterator[Any]:
        iterators = [self._iter_segment(segment.path) for segment in runs]
        skipped = 0
        taken = 0
        threshold: Optional[_Record] = None

        try:
            for record in self._merged(iterators):
                if skipped < self._offset:
                    skipped += 1
                    continue

                if taken < self._limit:
                    taken += 1
                    if taken == self._limit:
                        threshold = record
                    yield record.row
                    continue

                # The window is full.  Only the trailing rows tied with the
                # N-th emitted row survive; skipped rows are never revisited.
                if threshold is not None and _same_score(record, threshold):
                    yield record.row
                else:
                    break
        finally:
            for iterator in iterators:
                iterator.close()

    def _merged(self, iterators: list[Iterator[_Record]]) -> Iterator[_Record]:
        heap: list[tuple[_Record, int]] = []
        for index, iterator in enumerate(iterators):
            try:
                record = next(iterator)
            except StopIteration:
                continue
            heapq.heappush(heap, (record, index))
        self._note_peak(len(heap))

        while heap:
            record, index = heapq.heappop(heap)
            yield record
            try:
                following = next(iterators[index])
            except StopIteration:
                continue
            heapq.heappush(heap, (following, index))
            self._note_peak(len(heap))

    def _iter_segment(self, path: str) -> Iterator[_Record]:
        with open(path, "rb") as stream:
            while True:
                try:
                    record = self._codec.load(stream)
                except EOFError:
                    return
                yield record

    # ------------------------------------------------------------------
    # Segment / temp directory helpers
    # ------------------------------------------------------------------
    def _ensure_temp_dir(self) -> None:
        if self._temp_dir is not None:
            return
        parent = self._temp_parent
        if parent is not None:
            os.makedirs(parent, exist_ok=True)
        self._temp_dir = tempfile.mkdtemp(prefix="external-rank-", dir=parent)
        self.stats.temp_dir = self._temp_dir

    def _new_segment_path(self) -> str:
        if self._temp_dir is None:
            raise RuntimeError("temporary directory has not been created")
        self._segment_seq += 1
        return os.path.join(
            self._temp_dir, f"segment-{self._segment_seq:06d}.bin"
        )

    def _write_segment(self, path: str, records: list[_Record]) -> int:
        with open(path, "wb") as stream:
            for record in records:
                self._codec.dump(stream, record)
        return len(records)

    def _cleanup(self) -> None:
        temp_dir = self._temp_dir
        self._temp_dir = None
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def _note_peak(self, count: int) -> None:
        if count > self.stats.peak_buffered_records:
            self.stats.peak_buffered_records = count


def _same_score(left: _Record, right: _Record) -> bool:
    if left.is_empty != right.is_empty:
        return False
    if left.is_empty:
        return True
    return left.score == right.score
