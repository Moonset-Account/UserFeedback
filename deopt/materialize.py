"""Materialization of optimized-state values back into interpreter values.

Responsibilities:

* rebuild objects the optimizer allocated virtually *without* re-running their
  constructors (confirmed side effects must not be repeated);
* preserve aliasing: two references that shared one object in the optimized
  state resolve to one rebuilt object, including references across different
  inlined frames and multiple references within the same frame;
* restore cyclic object graphs;
* fail loudly, with frame/value coordinates, whenever a live value has no
  usable provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .interpreter import Runtime
from .ir import IRObject
from .sources import ConstSource, ReconstructSource, RegisterSource, Source

# Sentinel placed in a live-value map when the optimizer cannot say where a
# live value came from.  Recovery must refuse rather than invent a value.
MISSING: Any = object()


class RecoveryError(Exception):
    """Raised when interpreter state cannot be fully recovered.

    The error reports the frame and local/value that lacked provenance, plus
    the access path (which object/field chain led to it) so the optimizer dump
    can be diagnosed.  Raising this means *no* interpreter state has been
    published: materialization is atomic at the recovery boundary.
    """

    def __init__(self, frame: str, value: str, reason: str,
                 path: tuple[str, ...] = ()) -> None:
        where = " -> ".join((frame, *path, value))
        super().__init__(f"cannot recover value at {where}: {reason}")
        self.frame = frame
        self.value = value
        self.reason = reason
        self.path = path


@dataclass(frozen=True)
class PendingStore:
    """A field store the optimizer performed on a virtual object.

    The store was observable only to the optimized computation; during
    recovery it is replayed onto the rebuilt object.  It is a plain field
    assignment, never a constructor side effect.
    """

    vobj: str
    field: str
    source: Source


class Materializer:
    """Turns source descriptions into concrete runtime values."""

    def __init__(self, runtime: Runtime,
                 register_bank: dict[str, Any]) -> None:
        self._runtime = runtime
        self._registers = register_bank
        # Identity memos: one register / one virtual object => one value.
        self._register_memo: dict[str, Any] = {}
        self._object_memo: dict[str, IRObject] = {}

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #

    def materialize(self, source: Any, frame: str, value: str,
                    path: tuple[str, ...] = ()) -> Any:
        if source is None or source is MISSING:
            raise RecoveryError(frame, value, "no provenance recorded", path)

        if isinstance(source, ConstSource):
            return source.value

        if isinstance(source, RegisterSource):
            return self._materialize_register(source.register, frame,
                                              value, path)

        if isinstance(source, ReconstructSource):
            return self._materialize_object(source, frame, value, path)

        raise RecoveryError(
            frame, value, f"unknown source kind {type(source).__name__}", path
        )

    def apply_pending(self, stores: list[PendingStore], frame: str) -> None:
        """Replay eliminated field stores after all values exist.

        Running this last guarantees cyclic targets already have placeholder
        objects.  No allocation, and therefore no constructor effect, happens
        here.  When the optimized trace overwrote the same field more than
        once, only the *last* write survives -- exactly as ordinary stores
        semantics dictate -- so earlier intermediate writes are coalesced.
        """

        latest: dict[tuple[str, str], PendingStore] = {}
        for store in stores:
            latest[(store.vobj, store.field)] = store
        for (vobj, field_name), store in latest.items():
            target = self._object_memo.get(vobj)
            if target is None:
                raise RecoveryError(
                    frame, vobj,
                    "pending store targets an unknown virtual object",
                    (f"store.{field_name}",),
                )
            target.fields[field_name] = self.materialize(
                store.source, frame, vobj,
                (f"store.{field_name}",),
            )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _materialize_register(self, register: str, frame: str, value: str,
                              path: tuple[str, ...]) -> Any:
        if register in self._register_memo:
            return self._register_memo[register]
        if register not in self._registers:
            raise RecoveryError(
                frame, value,
                f"register {register!r} has no available value", path,
            )
        materialized = self._registers[register]
        self._register_memo[register] = materialized
        return materialized

    def _materialize_object(self, source: ReconstructSource, frame: str,
                            value: str, path: tuple[str, ...]) -> IRObject:
        # Alias + cycle handling: the memo is populated *before* descending
        # into fields, so back-edges resolve to this very object.
        existing = self._object_memo.get(source.vobj)
        if existing is not None:
            return existing

        obj = self._runtime.allocate(source.tag)
        self._object_memo[source.vobj] = obj
        for field_name, field_source in source.fields.items():
            obj.fields[field_name] = self.materialize(
                field_source, frame, value,
                path + (f"{source.vobj}.{field_name}",),
            )
        return obj
