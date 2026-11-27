"""Demand-driven row expansion over several nullable list sources.

The operator expands one input row into the cross product of the per-level
list elements.  Levels are applied left to right, like a chain of::

    FROM t
    LEFT JOIN LATERAL <level 0 source> AS lvl0 ON TRUE
    LEFT JOIN LATERAL <level 1 source> AS lvl1 ON TRUE   -- may read lvl0
    ...

Everything is pull based: nothing is read from a source until the caller asks
for a row, and a cursor may be saved after any emitted row and resumed against
the same input snapshot without materialising the remaining combinations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PADDED_ORDINAL",
    "Cursor",
    "CursorError",
    "Level",
    "RowCursor",
    "RowExpander",
    "Snapshot",
]


#: Ordinal value used for the single row that an ``outer`` level synthesises
#: when its list is NULL/empty.  Real elements always receive an integer
#: ordinal (``ordinal_start + position``), even when their value is ``None``.
PADDED_ORDINAL = None


class CursorError(ValueError):
    """Raised when a saved cursor cannot be replayed against a snapshot."""


Source = str | Callable[["Mapping[str, Any]", "Mapping[str, Any]"], "Iterable[Any] | None"]


@dataclass(frozen=True)
class Level:
    """One expansion level.

    Parameters
    ----------
    name:
        Output column holding the current element of this level.  Must not
        collide with an input column or with another output column.
    source:
        Either the name of an input column holding a nullable, *re-iterable*
        list, or a callable ``(row, bindings) -> iterable | None``.  A callable
        is invoked afresh on every pass (forward expansion and cursor replay),
        so it must be reproducible; returning a new generator on each call is
        fine.
    mode:
        ``"inner"`` drops every branch where this level's list is NULL or
        empty.  ``"outer"`` keeps exactly one branch for such a list, with the
        element bound to ``None`` and the ordinal set to :data:`PADDED_ORDINAL`.
    ordinal_name:
        Output column for this level's ordinal.  Defaults to
        ``f"{name}_ordinal"``.
    ordinal_start:
        Ordinal assigned to the first element of the list (default ``1``).
        Padded branches always use :data:`PADDED_ORDINAL`, never an integer.
    """

    name: str
    source: Source
    mode: str = "inner"
    ordinal_name: str | None = None
    ordinal_start: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Level.name must be a non-empty string")
        if isinstance(self.source, str):
            if not self.source:
                raise ValueError(f"Level {self.name!r}: source column name is empty")
        elif not callable(self.source):
            raise TypeError(
                f"Level {self.name!r}: source must be a column name or a callable"
            )
        if self.mode not in ("inner", "outer"):
            raise ValueError(
                f"Level {self.name!r}: mode must be 'inner' or 'outer', got {self.mode!r}"
            )
        if self.ordinal_name is None:
            object.__setattr__(self, "ordinal_name", f"{self.name}_ordinal")
        elif not isinstance(self.ordinal_name, str) or not self.ordinal_name:
            raise ValueError(f"Level {self.name!r}: ordinal_name must be a non-empty string")
        if self.ordinal_name == self.name:
            raise ValueError(
                f"Level {self.name!r}: ordinal_name must differ from the level name"
            )
        if (
            not isinstance(self.ordinal_start, int)
            or isinstance(self.ordinal_start, bool)
            or self.ordinal_start < 0
        ):
            raise ValueError(
                f"Level {self.name!r}: ordinal_start must be a non-negative integer"
            )


class Snapshot:
    """A re-iterable source of input rows.

    Accepts either

    * a factory callable returning a fresh iterator/iterable on each call, or
    * a re-iterable container (list, tuple, custom ``__iter__``).

    One-shot iterators (generators, ``iter(list)``, file objects, ...) are
    rejected: a cursor may be resumed later, which requires starting the input
    stream again.  Wrap a generator function instead of a generator object.
    """

    __slots__ = ("_factory", "_token")

    def __init__(
        self,
        rows: "Snapshot | Callable[[], Iterable[Any]] | Iterable[Any]",
        *,
        token: Any = None,
    ) -> None:
        if isinstance(rows, Snapshot):
            self._factory = rows._factory
            if token is None:
                token = rows._token
        elif callable(rows) and not isinstance(rows, (str, bytes, bytearray)):
            self._factory = rows
        elif isinstance(rows, (str, bytes, bytearray)):
            raise TypeError("Snapshot cannot be built from a string/bytes object")
        elif isinstance(rows, Iterable):
            probe = iter(rows)
            if probe is rows:
                raise TypeError(
                    "Snapshot needs a re-iterable row source or a factory callable; "
                    "a one-shot iterator cannot be replayed for cursor resumption"
                )
            self._factory = lambda: iter(rows)
        else:
            raise TypeError(
                "Snapshot needs a factory callable or a re-iterable iterable of rows"
            )
        self._token = token

    @property
    def token(self) -> Any:
        """Optional opaque identity used to detect replaying against other data."""
        return self._token

    def __iter__(self) -> Iterator[Any]:
        return iter(self._factory())


@dataclass(frozen=True)
class Cursor:
    """Value object describing a position *after* the last emitted row.

    A cursor is produced by :meth:`RowCursor.cursor` and consumed by
    :meth:`RowExpander.resume`.

    ``positions`` is ``None`` when no row has been emitted yet from the current
    input row; otherwise it holds one entry per level, where ``-1`` means
    "outer-padded branch" and a non-negative value is the zero-based element
    position in that level's current list.  ``row_index`` is the zero-based
    index of the input row the last emitted row came from, or — when
    ``positions`` is ``None`` — the index of the next input row to scan.
    """

    row_index: int = 0
    positions: tuple[int, ...] | None = None
    done: bool = False
    snapshot_token: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.row_index, int) or isinstance(self.row_index, bool):
            raise TypeError("Cursor.row_index must be an integer")
        if self.row_index < 0:
            raise ValueError("Cursor.row_index must be non-negative")
        if self.positions is not None:
            if not isinstance(self.positions, tuple):
                raise TypeError("Cursor.positions must be a tuple or None")
            for pos in self.positions:
                if not isinstance(pos, int) or isinstance(pos, bool) or pos < -1:
                    raise ValueError("Cursor.positions entries must be integers >= -1")
        if not isinstance(self.done, bool):
            raise TypeError("Cursor.done must be a bool")
        if self.done and self.positions is not None:
            raise ValueError("A finished cursor cannot carry positions")


class _Frame:
    """Live iteration state for one level of the current combination."""

    __slots__ = ("iterator", "pos")

    def __init__(self, iterator: Iterator[Any], pos: int) -> None:
        self.iterator = iterator
        self.pos = pos  # -1 == padded branch


def _close_iterator(iterator: Any) -> None:
    closer = getattr(iterator, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - defensive cleanup
            pass


def _close_from(frames: list["_Frame | None"], start: int) -> None:
    for idx in range(start, len(frames)):
        frame = frames[idx]
        if frame is not None:
            _close_iterator(frame.iterator)
            frames[idx] = None


class RowExpander:
    """Expand nullable list levels of an input row into a cross product."""

    def __init__(self, levels: Iterable[Level | Mapping[str, Any]]) -> None:
        normalized: list[Level] = []
        for level in levels:
            if isinstance(level, Level):
                normalized.append(level)
            elif isinstance(level, Mapping):
                normalized.append(Level(**dict(level)))
            else:
                raise TypeError("levels must contain Level objects or mappings")
        if not normalized:
            raise ValueError("RowExpander needs at least one level")

        names: set[str] = set()
        ordinals: set[str] = set()
        for level in normalized:
            if level.name in names:
                raise ValueError(f"duplicate level name {level.name!r}")
            names.add(level.name)
            if level.ordinal_name in ordinals:
                raise ValueError(f"duplicate ordinal column {level.ordinal_name!r}")
            ordinals.add(level.ordinal_name)
        overlap = names & ordinals
        if overlap:
            raise ValueError(
                "ordinal columns collide with level names: " + ", ".join(sorted(overlap))
            )

        self._levels: tuple[Level, ...] = tuple(normalized)

    # -- introspection ---------------------------------------------------
    @property
    def levels(self) -> tuple[Level, ...]:
        return self._levels

    @property
    def value_columns(self) -> tuple[str, ...]:
        return tuple(level.name for level in self._levels)

    @property
    def ordinal_columns(self) -> tuple[str, ...]:
        return tuple(level.ordinal_name for level in self._levels)  # type: ignore[misc]

    # -- public API ------------------------------------------------------
    def expand(
        self,
        rows: "Snapshot | Callable[[], Iterable[Any]] | Iterable[Any]",
        cursor: Cursor | None = None,
    ) -> "RowCursor":
        """Open a cursor over ``rows``.

        When ``cursor`` is supplied, the returned cursor starts immediately
        *after* that saved boundary.  ``rows`` must describe the same snapshot
        the cursor was captured from.
        """
        return RowCursor(self, Snapshot(rows), cursor)

    def resume(
        self,
        rows: "Snapshot | Callable[[], Iterable[Any]] | Iterable[Any]",
        cursor: Cursor,
    ) -> "RowCursor":
        """Shortcut for :meth:`expand` with a saved cursor."""
        return self.expand(rows, cursor)

    # -- internal expansion machinery ------------------------------------
    def _iter_items(
        self, level: Level, row: Mapping[str, Any], bindings: Mapping[str, Any]
    ) -> Iterator[Any]:
        if isinstance(level.source, str):
            try:
                items = row[level.source]
            except KeyError:
                raise KeyError(
                    f"level {level.name!r}: source column {level.source!r} "
                    "is missing from the input row"
                ) from None
            one_shot_guard = True
        else:
            items = level.source(row, bindings)
            one_shot_guard = False

        if items is None:
            return iter(())
        if isinstance(items, (str, bytes, bytearray)):
            raise TypeError(
                f"level {level.name!r} produced a string/bytes value; "
                "a list-like iterable (or None) is required"
            )
        try:
            iterator = iter(items)
        except TypeError:
            raise TypeError(
                f"level {level.name!r} produced a non-iterable {type(items).__name__}; "
                "a list-like iterable (or None) is required"
            ) from None
        if one_shot_guard and iterator is items:
            raise TypeError(
                f"level {level.name!r}: column {level.source!r} holds a one-shot "
                "iterator; use a re-iterable value or a callable source so cursors "
                "can be replayed"
            )
        return iterator

    def _fill(
        self,
        row: Mapping[str, Any],
        start: int,
        frames: list[_Frame | None],
        bindings: dict[str, Any],
    ) -> int:
        """Open every level from ``start`` downwards for the first branch.

        Returns ``-1`` on success.  Otherwise returns the index of the first
        ``inner`` level whose list was NULL/empty for the current prefix; all
        levels from that index downwards are left closed so the caller can try
        the next sibling of a shallower level.
        """
        for j in range(start, len(self._levels)):
            level = self._levels[j]
            old = frames[j]
            if old is not None:
                _close_iterator(old.iterator)
                frames[j] = None
            try:
                iterator = self._iter_items(level, row, bindings)
            except BaseException:
                _close_from(frames, j)
                raise
            try:
                value = next(iterator)
            except StopIteration:
                if level.mode == "inner":
                    _close_iterator(iterator)
                    _close_from(frames, j)
                    return j
                frames[j] = _Frame(iterator, -1)
                bindings[level.name] = None
                continue
            except BaseException:
                _close_iterator(iterator)
                _close_from(frames, j)
                raise
            frames[j] = _Frame(iterator, 0)
            bindings[level.name] = value
        return -1

    def _next_combination(
        self,
        row: Mapping[str, Any],
        frames: list[_Frame | None],
        bindings: dict[str, Any],
        k: int,
    ) -> bool:
        """Advance the combination, treating level ``k`` as the odometer head.

        ``frames[0..k]`` must currently hold an open, valid prefix.  Tries the
        next sibling at the deepest level first; whenever a shallower level
        moves, the levels below it are refilled lazily.
        """
        while k >= 0:
            frame = frames[k]
            if frame is None:
                k -= 1
                continue
            if frame.pos < 0:  # padded branches have no siblings
                k -= 1
                continue
            try:
                value = next(frame.iterator)
            except StopIteration:
                _close_iterator(frame.iterator)
                frames[k] = None
                k -= 1
                continue
            frame.pos += 1
            bindings[self._levels[k].name] = value
            failed_at = self._fill(row, k + 1, frames, bindings)
            if failed_at < 0:
                return True
            # The deeper level(s) were empty; try the next sibling of the
            # deepest level that still has one.
            k = failed_at - 1
        return False

    def _base_columns(self, row: Any) -> dict[str, Any]:
        if not isinstance(row, Mapping):
            raise TypeError(
                "each input row must be a mapping of column name -> value, "
                f"got {type(row).__name__}"
            )
        base = dict(row)
        for level in self._levels:
            if level.name in base:
                raise ValueError(
                    f"level name {level.name!r} collides with an input column"
                )
            if level.ordinal_name in base:
                raise ValueError(
                    f"ordinal column {level.ordinal_name!r} collides with an input column"
                )
        return base

    def _make_row(
        self,
        base: Mapping[str, Any],
        frames: list[_Frame | None],
        bindings: Mapping[str, Any],
    ) -> dict[str, Any]:
        out = dict(base)
        for level, frame in zip(self._levels, frames):
            assert frame is not None
            if frame.pos < 0:
                out[level.name] = None
                out[level.ordinal_name] = PADDED_ORDINAL  # type: ignore[index]
            else:
                out[level.name] = bindings[level.name]
                out[level.ordinal_name] = level.ordinal_start + frame.pos  # type: ignore[index]
        return out


class RowCursor:
    """A pull-based cursor yielding expanded rows from a snapshot."""

    __slots__ = (
        "_expander",
        "_row_iter",
        "_token",
        "_row_index",
        "_row",
        "_base",
        "_frames",
        "_bindings",
        "_done",
        "_emitted",
    )

    def __init__(
        self,
        expander: RowExpander,
        snapshot: Snapshot,
        cursor: Cursor | None = None,
    ) -> None:
        self._expander = expander
        self._row_iter: Iterator[Any] = iter(snapshot)
        self._token = snapshot.token
        self._row_index = -1
        self._row: Mapping[str, Any] | None = None
        self._base: dict[str, Any] | None = None
        self._frames: list[_Frame | None] | None = None
        self._bindings: dict[str, Any] | None = None
        self._done = False
        self._emitted = 0
        if cursor is not None:
            if not isinstance(cursor, Cursor):
                raise TypeError("cursor must be a Cursor instance")
            self._restore(cursor)

    # -- cursor state ----------------------------------------------------
    def _restore(self, cursor: Cursor) -> None:
        n = len(self._expander.levels)
        if cursor.done:
            self._done = True
            self._row_index = cursor.row_index - 1 if cursor.row_index else -1
            return
        if (
            cursor.snapshot_token is not None
            and self._token is not None
            and cursor.snapshot_token != self._token
        ):
            raise CursorError("cursor belongs to a different input snapshot")
        if cursor.positions is not None and len(cursor.positions) != n:
            raise CursorError(
                f"cursor has {len(cursor.positions)} positions but the expander "
                f"has {n} levels"
            )

        for _ in range(cursor.row_index):
            try:
                next(self._row_iter)
            except StopIteration:
                raise CursorError(
                    "cursor row_index is past the end of the snapshot"
                ) from None

        if cursor.positions is None:
            # Cursor sits before the first row of ``cursor.row_index``.
            self._row_index = cursor.row_index - 1
            return

        try:
            row = next(self._row_iter)
        except StopIteration:
            raise CursorError("cursor points past the end of the snapshot") from None
        self._row_index = cursor.row_index
        self._row = row
        self._base = self._expander._base_columns(row)

        frames: list[_Frame | None] = [None] * n
        bindings: dict[str, Any] = {}
        for j, level in enumerate(self._expander.levels):
            iterator = self._expander._iter_items(level, row, bindings)
            pos = cursor.positions[j]
            if pos == -1:
                if level.mode != "outer":
                    raise CursorError(
                        f"cursor marks level {level.name!r} as padded but its "
                        "mode is 'inner'"
                    )
                try:
                    next(iterator)
                except StopIteration:
                    pass
                else:
                    raise CursorError(
                        f"cursor marks level {level.name!r} as padded but the "
                        "snapshot source is not empty at that row"
                    )
                frames[j] = _Frame(iterator, -1)
                bindings[level.name] = None
                continue
            for _ in range(pos):
                try:
                    next(iterator)
                except StopIteration:
                    raise CursorError(
                        f"cursor position {pos} exceeds the length of level "
                        f"{level.name!r}'s list"
                    ) from None
            try:
                value = next(iterator)
            except StopIteration:
                raise CursorError(
                    f"cursor position {pos} exceeds the length of level "
                    f"{level.name!r}'s list"
                ) from None
            frames[j] = _Frame(iterator, pos)
            bindings[level.name] = value

        self._frames = frames
        self._bindings = bindings

    def cursor(self) -> Cursor:
        """Return a value cursor at the current output boundary."""
        if self._done:
            return Cursor(
                row_index=max(self._row_index + 1, 0),
                positions=None,
                done=True,
                snapshot_token=self._token,
            )
        if self._frames is None:
            return Cursor(
                row_index=self._row_index + 1,
                positions=None,
                done=False,
                snapshot_token=self._token,
            )
        positions = []
        for frame in self._frames:
            if frame is None:
                raise RuntimeError("internal error: incomplete expansion state")
            positions.append(frame.pos)
        return Cursor(
            row_index=self._row_index,
            positions=tuple(positions),
            done=False,
            snapshot_token=self._token,
        )

    # -- pulling ---------------------------------------------------------
    def next_row(self) -> dict[str, Any]:
        if self._done:
            raise StopIteration
        exp = self._expander
        while True:
            if self._frames is None:
                try:
                    row = next(self._row_iter)
                except StopIteration:
                    self._done = True
                    self._row = None
                    raise StopIteration from None
                self._row_index += 1
                self._row = row
                self._base = exp._base_columns(row)
                frames: list[_Frame | None] = [None] * len(exp.levels)
                bindings: dict[str, Any] = {}
                failed_at = exp._fill(row, 0, frames, bindings)
                if failed_at < 0:
                    self._frames = frames
                    self._bindings = bindings
                    self._emitted += 1
                    return exp._make_row(self._base, frames, bindings)
                # The first branch was empty; look for the next sibling of a
                # shallower level before giving up on this input row.
                if not exp._next_combination(
                    row, frames, bindings, failed_at - 1
                ):
                    self._row = None
                    self._base = None
                    continue
                self._frames = frames
                self._bindings = bindings
                self._emitted += 1
                return exp._make_row(self._base, frames, bindings)

            assert self._row is not None
            assert self._base is not None
            assert self._bindings is not None
            if exp._next_combination(
                self._row, self._frames, self._bindings, len(exp.levels) - 1
            ):
                self._emitted += 1
                return exp._make_row(self._base, self._frames, self._bindings)
            # Current input row exhausted; move to the next one.
            _close_from(self._frames, 0)
            self._frames = None
            self._bindings = None
            self._row = None
            self._base = None

    def next_batch(self, size: int) -> list[dict[str, Any]]:
        """Pull at most ``size`` rows (exactly ``size`` unless exhausted)."""
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("batch size must be a non-negative integer")
        batch: list[dict[str, Any]] = []
        for _ in range(size):
            try:
                batch.append(self.next_row())
            except StopIteration:
                break
        return batch

    def close(self) -> None:
        """Stop pulling and release any live source iterators."""
        if self._frames:
            _close_from(self._frames, 0)
        self._frames = None
        self._bindings = None
        self._row = None
        self._base = None
        self._done = True

    @property
    def emitted(self) -> int:
        return self._emitted

    @property
    def done(self) -> bool:
        return self._done

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while True:
            try:
                yield self.next_row()
            except StopIteration:
                return

    __next__ = next_row
