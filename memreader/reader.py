"""Object-level reads and pointer-graph traversal, bound to one snapshot.

Every field of a read object yields a ``FieldResult`` carrying either the
decoded value or a status (``missing_page`` / ``conflict`` /
``out_of_bounds`` / ``dependency_failed``) plus the source intervals that
were (or could be) used.  Fields already confirmed stay in the result even
when later fields hit missing pages.

Budgets
-------
* ``max_field_length`` -- maximum byte length of any single field,
  including the computed total of a variable-length array.  A field whose
  declared/computed length exceeds the budget is reported
  ``out_of_bounds`` and not read; parsing of later fields continues
  because their offsets are still computable.
* ``max_objects`` -- maximum number of objects produced by one traversal.

Traversal follows one pointer field from object to object inside a single
``(generation, snapshot_id)`` context.  Object identity is the triple
``(generation, snapshot_id, va)``; cycles are detected on that identity,
so the same VA in a different generation or snapshot is a *different*
object and is never merged into the current traversal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import FieldStatus, SnapshotStore
from .layout import ArrayField, BytesField, PointerField, StructDesc, UIntField


@dataclass(frozen=True)
class Budgets:
    max_field_length: int = 4096
    max_objects: int = 1024


@dataclass
class FieldResult:
    name: str
    status: FieldStatus
    value: object = None
    sources: tuple = ()
    missing: tuple = ()
    conflicts: tuple = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is FieldStatus.OK


@dataclass
class ObjectResult:
    struct_name: str
    identity: tuple  # (generation, snapshot_id, va)
    fields: dict = field(default_factory=dict)
    truncated: bool = False  # True if a variable-length field hid later offsets

    @property
    def va(self) -> int:
        return self.identity[2]

    @property
    def status(self) -> FieldStatus:
        for f in self.fields.values():
            if not f.ok:
                return f.status
        return FieldStatus.OK

    @property
    def ok(self) -> bool:
        return self.status is FieldStatus.OK


@dataclass
class TraversalResult:
    objects: list = field(default_factory=list)
    stop_reason: str = "null_pointer"  # null_pointer|cycle|object_budget|next_field_unavailable
    cycle_identity: tuple | None = None

    @property
    def cycle_detected(self) -> bool:
        return self.stop_reason == "cycle"

    @property
    def budget_exceeded(self) -> bool:
        return self.stop_reason == "object_budget"

    @property
    def terminated_normally(self) -> bool:
        return self.stop_reason == "null_pointer"


class ObjectReader:
    def __init__(self, store: SnapshotStore, budgets: Budgets | None = None):
        self.store = store
        self.budgets = budgets or Budgets()

    # -- single object -----------------------------------------------------

    def read_object(
        self, generation: int, snapshot_id: str, struct: StructDesc, va: int
    ) -> ObjectResult:
        fields: dict[str, FieldResult] = {}
        offset = va
        offset_known = True
        for fdesc in struct.fields:
            if not offset_known:
                fields[fdesc.name] = FieldResult(
                    fdesc.name,
                    FieldStatus.DEPENDENCY_FAILED,
                    detail="offset unknown: preceding variable-length field unresolved",
                )
                continue
            size = self._field_size(fdesc, fields)
            if size is None:
                fields[fdesc.name] = FieldResult(
                    fdesc.name,
                    FieldStatus.DEPENDENCY_FAILED,
                    detail=f"length depends on field '{fdesc.count_from}' which is not OK",
                )
                offset_known = False
                continue
            if size > self.budgets.max_field_length:
                fields[fdesc.name] = FieldResult(
                    fdesc.name,
                    FieldStatus.OUT_OF_BOUNDS,
                    detail=(
                        f"field length {size} exceeds budget "
                        f"{self.budgets.max_field_length}"
                    ),
                )
                offset += size
                continue
            raw = self.store.read_bytes(generation, snapshot_id, offset, size)
            if raw.status is not FieldStatus.OK:
                fields[fdesc.name] = FieldResult(
                    fdesc.name, raw.status, None, raw.sources, raw.missing, raw.conflicts
                )
                offset += size
                continue
            fields[fdesc.name] = FieldResult(
                fdesc.name,
                FieldStatus.OK,
                self._decode(fdesc, raw.data),
                raw.sources,
            )
            offset += size
        return ObjectResult(
            struct.name,
            (generation, snapshot_id, va),
            fields,
            truncated=not offset_known,
        )

    # -- traversal ---------------------------------------------------------

    def traverse(
        self,
        generation: int,
        snapshot_id: str,
        struct: StructDesc,
        va: int,
        next_field: str,
    ) -> TraversalResult:
        visited: set[tuple] = set()
        objects: list[ObjectResult] = []
        cur = va
        while cur != 0:
            ident = (generation, snapshot_id, cur)
            if ident in visited:
                return TraversalResult(objects, "cycle", cycle_identity=ident)
            if len(objects) >= self.budgets.max_objects:
                return TraversalResult(objects, "object_budget")
            visited.add(ident)
            obj = self.read_object(generation, snapshot_id, struct, cur)
            objects.append(obj)
            nf = obj.fields.get(next_field)
            if nf is None or not nf.ok:
                return TraversalResult(objects, "next_field_unavailable")
            cur = nf.value
        return TraversalResult(objects, "null_pointer")

    # -- helpers -----------------------------------------------------------

    def _field_size(self, fdesc, fields: dict) -> int | None:
        if isinstance(fdesc, UIntField):
            return fdesc.size
        if isinstance(fdesc, BytesField):
            return fdesc.length
        if isinstance(fdesc, PointerField):
            return fdesc.size
        if isinstance(fdesc, ArrayField):
            dep = fields.get(fdesc.count_from)
            if dep is None or not dep.ok or not isinstance(dep.value, int):
                return None
            return dep.value * self._field_size(fdesc.element, fields)
        raise TypeError(f"unknown field descriptor: {fdesc!r}")

    def _decode(self, fdesc, data: bytes):
        if isinstance(fdesc, UIntField):
            return int.from_bytes(data, fdesc.byteorder, signed=fdesc.signed)
        if isinstance(fdesc, PointerField):
            return int.from_bytes(data, fdesc.byteorder)
        if isinstance(fdesc, BytesField):
            return bytes(data)
        if isinstance(fdesc, ArrayField):
            esz = self._field_size(fdesc.element, {})
            return [
                self._decode(fdesc.element, data[i * esz : (i + 1) * esz])
                for i in range(len(data) // esz)
            ]
        raise TypeError(f"unknown field descriptor: {fdesc!r}")
