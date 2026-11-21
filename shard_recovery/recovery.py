"""Recover one new rank's tensors from old, differently-sharded checkpoint shards.

The entry point :func:`recover_rank` takes a target logical rectangle and an
old checkpoint manifest, then rebuilds every rank-2 tensor (weights and each
per-element optimizer state, which shares the weight's shape) inside that
rectangle.

Guarantees:

* Only shards intersecting the target are opened, and only bytes of valid
  float32 elements inside the intersection are read; row-end and end-of-file
  padding are skipped with seeks.  Every touched half-open byte interval is
  reported per file for auditing.
* The intersecting shards must tile the target exactly once.  Missing blocks
  or overlapping blocks are reported with their *logical* ranges.
* Manifest geometry, shard headers and actual file sizes must agree on the
  logical rectangle, physical stride, valid shape and padding.  Any mismatch
  is reported as a shape mismatch with the affected logical range.
* On any failure the rank is not published: the recovered result carries the
  structured errors and :func:`publish_recovery` refuses to write anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

from .checkpoint import (
    DTYPE_NAME,
    MANIFEST_FORMAT_VERSION,
    MANIFEST_FILENAME,
    load_manifest,
    manifest_tensor_shapes,
    shard_abs_path,
)
from .errors import ManifestError, RecoveryPublishError
from .geometry import Rect, cover_rect
from .shard import ByteRange, ShardFile, write_shard

__all__ = [
    "RecoveryError",
    "ShardAccess",
    "TensorRecovery",
    "RecoveryResult",
    "recover_rank",
    "publish_recovery",
]

ERROR_MISSING = "missing"
ERROR_OVERLAP = "overlap"
ERROR_SHAPE_MISMATCH = "shape_mismatch"
ERROR_FORMAT = "format"


@dataclass(frozen=True)
class RecoveryError:
    """Structured failure for one tensor; the rank is never published."""

    tensor: str
    kind: str
    ranges: Tuple[Rect, ...]
    message: str

    def __str__(self) -> str:
        spans = ", ".join(str(r) for r in self.ranges) or "<no logical range>"
        return f"[{self.tensor}] {self.kind} at {spans}: {self.message}"


@dataclass(frozen=True)
class ShardAccess:
    """Audit record: which old shard contributed and which bytes were read."""

    path: str
    rect: Rect
    accessed_ranges: Tuple[ByteRange, ...]


@dataclass(frozen=True)
class TensorRecovery:
    """Recovered row-major values for one tensor plus the read audit trail."""

    name: str
    rows: int
    cols: int
    values: Tuple[Tuple[float, ...], ...]
    accessed: Tuple[ShardAccess, ...]


@dataclass(frozen=True)
class RecoveryResult:
    target_rank: int
    target_rect: Rect
    ok: bool
    tensors: Tuple[TensorRecovery, ...]
    errors: Tuple[RecoveryError, ...]
    manifest_path: str | None = None
    output_dir: str | None = None

    def tensor(self, name: str) -> TensorRecovery:
        for recovered in self.tensors:
            if recovered.name == name:
                return recovered
        raise KeyError(name)


def _error(tensor: str, kind: str, ranges: Sequence[Rect], message: str) -> RecoveryError:
    return RecoveryError(tensor, kind, tuple(ranges), message)


def _recover_tensor(
    name: str,
    tensor_meta: Mapping[str, Any],
    target: Rect,
    manifest_path: str,
) -> TensorRecovery | RecoveryError:
    rows = int(tensor_meta["rows"])
    cols = int(tensor_meta["cols"])

    full = Rect.full(rows, cols)
    if not full.contains(target):
        return _error(
            name,
            ERROR_SHAPE_MISMATCH,
            (target,),
            f"target {target} escapes tensor shape {full}",
        )

    records = list(tensor_meta["shards"])
    rects = [Rect(*record["rect"]) for record in records]
    report = cover_rect(rects, target)

    if report.uncovered:
        return _error(
            name,
            ERROR_MISSING,
            report.uncovered,
            "old shards leave parts of the target uncovered",
        )
    if report.overlaps:
        overlap_ranges = tuple(overlap[2] for overlap in report.overlaps)
        return _error(
            name,
            ERROR_OVERLAP,
            overlap_ranges,
            "old shards overlap inside the target",
        )

    # Coverage is proven.  Open only the intersecting shards, cross-check the
    # on-disk headers against the manifest, then read the intersection.
    assembled: Dict[int, Dict[int, float]] = {}
    audits: list[ShardAccess] = []

    for record, rect in zip(records, rects):
        wanted = rect.intersect(target)
        if wanted.is_empty:
            continue  # disjoint shard: never touched

        path = shard_abs_path(manifest_path, record["path"])
        try:
            shard = ShardFile(path)
            shard.open()
        except OSError as exc:  # pragma: no cover - defensive
            return _error(name, ERROR_FORMAT, (wanted,), f"cannot open {path!r}: {exc}")
        except Exception as exc:
            return _error(name, ERROR_FORMAT, (wanted,), f"{path!r}: {exc}")

        with shard:
            header = shard.header

            if header.rect != rect:
                return _error(
                    name,
                    ERROR_SHAPE_MISMATCH,
                    (rect,),
                    f"manifest rect {rect} disagrees with on-disk rect {header.rect} "
                    f"in {path!r}",
                )
            if header.rows != rows or header.cols != cols:
                return _error(
                    name,
                    ERROR_SHAPE_MISMATCH,
                    (rect,),
                    f"on-disk physical shape {header.rows}x{header.cols} disagrees with "
                    f"manifest shape {rows}x{cols} in {path!r}",
                )
            if "stride_floats" in record and header.stride_floats != int(record["stride_floats"]):
                return _error(
                    name,
                    ERROR_SHAPE_MISMATCH,
                    (rect,),
                    f"on-disk stride {header.stride_floats} disagrees with manifest "
                    f"record {record['stride_floats']} in {path!r}",
                )
            if "end_padding" in record and header.end_padding != int(record["end_padding"]):
                return _error(
                    name,
                    ERROR_SHAPE_MISMATCH,
                    (rect,),
                    f"on-disk end padding {header.end_padding} disagrees with manifest "
                    f"record {record['end_padding']} in {path!r}",
                )

            try:
                block = shard.read_rect(wanted)
            except Exception as exc:
                return _error(name, ERROR_FORMAT, (wanted,), f"{path!r}: {exc}")

            audits.append(
                ShardAccess(
                    path=path,
                    rect=wanted,
                    accessed_ranges=shard.accessed_ranges,
                )
            )

        for local_row, row_values in enumerate(block):
            global_row = wanted.row0 + local_row
            row_map = assembled.setdefault(global_row, {})
            for local_col, value in enumerate(row_values):
                global_col = wanted.col0 + local_col
                # cover_rect already proved uniqueness; guard anyway.
                if global_col in row_map:
                    return _error(
                        name,
                        ERROR_OVERLAP,
                        (Rect(global_row, global_col, global_row + 1, global_col + 1),),
                        f"duplicate element while assembling from {path!r}",
                    )
                row_map[global_col] = value

    values = tuple(
        tuple(assembled[r][c] for c in range(target.col0, target.col1))
        for r in range(target.row0, target.row1)
    )
    return TensorRecovery(
        name=name,
        rows=target.rows,
        cols=target.cols,
        values=values,
        accessed=tuple(audits),
    )


def recover_rank(
    manifest_path: str,
    target_rank: int,
    target_rect: Rect | Sequence[int],
) -> RecoveryResult:
    """Recover all tensors of one rank inside ``target_rect``.

    The result is always returned (never raised) with ``ok`` indicating
    success.  On failure ``tensors`` is empty, ``errors`` names every logical
    range that caused the refusal, and no publishing occurs.
    """

    target = Rect.coerce(target_rect)
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return RecoveryResult(
            target_rank=target_rank,
            target_rect=target,
            ok=False,
            tensors=(),
            errors=(RecoveryError("<manifest>", ERROR_FORMAT, (), str(exc)),),
            manifest_path=None,
        )

    shapes = manifest_tensor_shapes(manifest)
    reference_shape = next(iter(shapes.values()))
    if not Rect.full(*reference_shape).contains(target):
        return RecoveryResult(
            target_rank=target_rank,
            target_rect=target,
            ok=False,
            tensors=(),
            errors=(
                RecoveryError(
                    "<manifest>",
                    ERROR_SHAPE_MISMATCH,
                    (target,),
                    f"target {target} escapes reference tensor shape {reference_shape}",
                ),
            ),
            manifest_path=None,
        )

    recovered_tensors = []
    errors = []
    for name, tensor_meta in manifest["tensors"].items():
        outcome = _recover_tensor(name, tensor_meta, target, manifest_path)
        if isinstance(outcome, RecoveryError):
            errors.append(outcome)
        else:
            recovered_tensors.append(outcome)

    if errors:
        # Refuse to publish: drop the assembled values from the public result.
        return RecoveryResult(
            target_rank=target_rank,
            target_rect=target,
            ok=False,
            tensors=(),
            errors=tuple(errors),
            manifest_path=None,
        )

    return RecoveryResult(
        target_rank=target_rank,
        target_rect=target,
        ok=True,
        tensors=tuple(recovered_tensors),
        errors=(),
        manifest_path=os.path.abspath(manifest_path),
    )


def publish_recovery(result: RecoveryResult, output_dir: str) -> RecoveryResult:
    """Write a recovered rank as a new single-shard-per-tensor checkpoint.

    Refuses (and creates nothing) when ``result.ok`` is false, so a rank with
    missing/overlapping/inconsistent blocks can never be published.
    """

    if not result.ok:
        problems = "; ".join(str(err) for err in result.errors) or "unknown recovery failure"
        raise RecoveryPublishError(f"rank {result.target_rank} not publishable: {problems}")

    if os.path.exists(output_dir):
        if not os.path.isdir(output_dir) or os.listdir(output_dir):
            raise RecoveryPublishError(
                f"output directory {output_dir!r} exists and is not empty"
            )
    os.makedirs(output_dir, exist_ok=True)

    try:
        manifest_tensors: Dict[str, Any] = {}
        for recovered in result.tensors:
            rect = Rect(0, 0, recovered.rows, recovered.cols)
            relative = f"{recovered.name}.rank{result.target_rank}.cksh"
            header = write_shard(
                os.path.join(output_dir, relative),
                rows=recovered.rows,
                cols=recovered.cols,
                rect=rect,
                values=[list(row) for row in recovered.values],
            )
            manifest_tensors[recovered.name] = {
                "rows": recovered.rows,
                "cols": recovered.cols,
                "source_target_rect": result.target_rect.as_tuple(),
                "shards": [
                    {
                        "path": relative,
                        "rect": rect.as_tuple(),
                        "stride_floats": header.stride_floats,
                        "end_padding": header.end_padding,
                    }
                ],
            }

        manifest = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "dtype": DTYPE_NAME,
            "target_rank": result.target_rank,
            "tensors": manifest_tensors,
        }
        manifest_path = os.path.join(output_dir, MANIFEST_FILENAME)
        with open(manifest_path, "w", encoding="utf-8") as file_obj:
            json.dump(manifest, file_obj, indent=2, sort_keys=True)
            file_obj.write("\n")
    except Exception as exc:
        raise RecoveryPublishError(f"failed while writing recovered rank: {exc}") from exc

    return RecoveryResult(
        target_rank=result.target_rank,
        target_rect=result.target_rect,
        ok=True,
        tensors=result.tensors,
        errors=(),
        manifest_path=os.path.abspath(manifest_path),
        output_dir=os.path.abspath(output_dir),
    )
