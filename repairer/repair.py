"""One-way repair driven by binary-prefix range summaries.

The repairer compares a *source* snapshot with a *target* snapshot over the
fixed 16-bit key space and mutates the target store so that its contents
become equal to the source's.  Both starting snapshots are taken once before
any write and never change; all decisions are based on that fixed starting
state.

Traversal rules (depth-first over the fixed left/right ``"0"`` / ``"1"``
partition):

1. If the source and target summaries of a prefix are equal, the whole range
   already matches and is skipped without reading any object body and without
   descending further.
2. On mismatch above single-key depth, descend into both fixed children.
3. At single-key depth (length-16 prefix):
   * source summary equals the empty-leaf digest -> the key is confirmed
     missing in the source, so delete it from the target if present;
   * otherwise read the source value (the only body read in the entire
     algorithm) and put it to the target, overwriting whatever is there.

The target's object bodies are never read: summary digests alone decide
everything target-side.  When the two trees have completely different
folding shapes but differ in just one key, exactly one source body read
happens, for that key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .digest import KEY_BITS, empty_leaf_digest
from .store import Store, StoreSnapshot


@dataclass(frozen=True)
class Repair:
    """A single repair action applied to the target."""

    key: int
    action: str  # "put" (overwrite/create) or "delete" (target-only key)
    value: Optional[bytes]  # present for "put", always None for "delete"


@dataclass
class RepairReport:
    """Outcome of a repair run.

    ``repairs`` lists the actions in application order; ``probed_prefixes``
    lists every prefix whose range summary was read (both sides share the
    same traversal, so the list is recorded once).  Snapshot counters are
    also exposed for per-side read amplification checks.
    """

    repairs: List[Repair] = field(default_factory=list)
    probed_prefixes: List[str] = field(default_factory=list)
    source_snapshot: Optional[StoreSnapshot] = None
    target_snapshot: Optional[StoreSnapshot] = None

    @property
    def put_count(self) -> int:
        return sum(1 for r in self.repairs if r.action == "put")

    @property
    def delete_count(self) -> int:
        return sum(1 for r in self.repairs if r.action == "delete")

    @property
    def repair_count(self) -> int:
        return len(self.repairs)

    @property
    def source_body_reads(self) -> tuple:
        return () if self.source_snapshot is None else self.source_snapshot.body_reads

    @property
    def target_body_reads(self) -> tuple:
        return () if self.target_snapshot is None else self.target_snapshot.body_reads


def reconcile(
    source: Store,
    target: Store,
    *,
    apply: bool = True,
) -> RepairReport:
    """Repair ``target`` so it matches ``source`` and return the change set.

    Snapshots of both sides are captured up front and held fixed for the whole
    run; target mutations go to the live store and cannot perturb the
    snapshots.  When ``apply`` is false the report is computed without
    touching the target store (a dry run).
    """
    source_snap = source.snapshot()
    target_snap = target.snapshot()
    report = RepairReport(
        source_snapshot=source_snap,
        target_snapshot=target_snap,
    )
    empty_leaf = empty_leaf_digest()

    def walk(prefix: str) -> None:
        report.probed_prefixes.append(prefix)
        src_digest = source_snap.summary(prefix)
        tgt_digest = target_snap.summary(prefix)
        if src_digest == tgt_digest:
            # Whole range matches: skip it, no body reads, no descent.
            return

        depth = len(prefix)
        if depth == KEY_BITS:
            # Single-key range and summaries differ.  The target side needed
            # no body read: its digest already told us it disagrees.
            key = int(prefix, 2)
            if src_digest == empty_leaf:
                # Confirmed missing in the source purely from the summary;
                # remove the target-only key.
                report.repairs.append(Repair(key=key, action="delete", value=None))
                if apply:
                    target.delete(key)
                return

            # Source holds a value: the one and only source body read.
            value = source_snap.get(key)
            if value is None:  # defensive: digest promised a present key
                raise RuntimeError(
                    "source summary indicated a present key but get() returned None"
                )
            report.repairs.append(Repair(key=key, action="put", value=value))
            if apply:
                target.put(key, value)
            return

        # Mismatch above leaf level: split along the fixed partition.
        walk(prefix + "0")
        walk(prefix + "1")

    walk("")
    return report
