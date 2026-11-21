"""Checkpoint manifests and helpers for building local synthetic checkpoints.

A checkpoint directory contains one JSON manifest plus one or more shard files
referenced by relative path.  The manifest makes every shard *locatable*
without scanning the directory; each record also carries the shard's logical
rectangle so coverage can be reasoned about before any file is opened.

Manifest schema (version 1)::

    {
      "format_version": 1,
      "dtype": "float32-le",
      "tensors": {
        "<tensor name>": {
          "rows": <int>, "cols": <int>,
          "shards": [
            {"path": "<relative path>", "rect": [row0, col0, row1, col1]},
            ...
          ]
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence

from .errors import ManifestError
from .geometry import Rect, cover_rect
from .shard import ShardHeader, write_shard

__all__ = [
    "MANIFEST_FORMAT_VERSION",
    "DTYPE_NAME",
    "ShardPlan",
    "TensorSource",
    "MANIFEST_FILENAME",
    "build_checkpoint",
    "load_manifest",
    "manifest_tensor_shapes",
    "manifest_shard_records",
    "shard_abs_path",
]

MANIFEST_FORMAT_VERSION = 1
DTYPE_NAME = "float32-le"
MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class ShardPlan:
    """Plan for one shard when building a synthetic checkpoint."""

    rect: Rect | Sequence[int]
    stride_floats: int | None = None
    end_padding: int = 0


@dataclass(frozen=True)
class TensorSource:
    """A full in-memory matrix plus the shards it should be cut into."""

    rows: int
    cols: int
    values: Sequence[Sequence[float]]
    plans: Sequence[ShardPlan]


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate manifest structure independently of the referenced files."""

    _check(isinstance(manifest, Mapping), "manifest must be a JSON object")
    _check(
        manifest.get("format_version") == MANIFEST_FORMAT_VERSION,
        f"unsupported manifest format_version, expected {MANIFEST_FORMAT_VERSION}",
    )
    _check(manifest.get("dtype") == DTYPE_NAME, f"unsupported dtype, expected {DTYPE_NAME}")
    tensors = manifest.get("tensors")
    _check(isinstance(tensors, Mapping) and bool(tensors), "manifest needs non-empty 'tensors'")

    for name, tensor in tensors.items():
        _check(isinstance(tensor, Mapping), f"tensor {name!r} must be an object")
        rows = tensor.get("rows")
        cols = tensor.get("cols")
        _check(isinstance(rows, int) and rows > 0, f"tensor {name!r} has bad rows")
        _check(isinstance(cols, int) and cols > 0, f"tensor {name!r} has bad cols")
        shards = tensor.get("shards")
        _check(isinstance(shards, list) and shards, f"tensor {name!r} needs shard records")
        for index, record in enumerate(shards):
            _check(isinstance(record, Mapping), f"shard {index} of {name!r} must be an object")
            _check(isinstance(record.get("path"), str) and record["path"],
                   f"shard {index} of {name!r} has bad path")
            rect = record.get("rect")
            _check(
                isinstance(rect, list) and len(rect) == 4
                and all(isinstance(v, int) for v in rect),
                f"shard {index} of {name!r} has bad rect",
            )
            logical = Rect(*rect)
            _check(
                logical.row0 >= 0 and logical.col0 >= 0
                and logical.row1 <= rows and logical.col1 <= cols,
                f"shard {index} of {name!r} rect escapes tensor shape",
            )


def build_checkpoint(
    root: str,
    tensors: Mapping[str, TensorSource],
    *,
    manifest_name: str = MANIFEST_FILENAME,
) -> str:
    """Cut full matrices into shard files and write a manifest.

    The shard plans of every tensor must tile its full logical matrix exactly
    once; otherwise :class:`ManifestError` is raised and nothing partial is
    promised.  Returns the absolute manifest path.
    """

    if not tensors:
        raise ManifestError("at least one tensor is required")

    os.makedirs(root, exist_ok=True)
    manifest_tensors: Dict[str, Any] = {}

    for name, source in tensors.items():
        if len(source.values) != source.rows or any(
            len(row) != source.cols for row in source.values
        ):
            raise ManifestError(f"tensor {name!r} values do not match its shape")
        if not source.plans:
            raise ManifestError(f"tensor {name!r} needs at least one shard plan")

        rects = [Rect.coerce(plan.rect) for plan in source.plans]
        report = cover_rect(rects, Rect.full(source.rows, source.cols))
        if not report.ok:
            details = [str(r) for r in report.uncovered]
            details.extend(str(overlap[2]) for overlap in report.overlaps)
            raise ManifestError(
                f"plans for tensor {name!r} do not tile its full matrix: {details}"
            )

        records = []
        for index, (plan, rect) in enumerate(zip(source.plans, rects)):
            sub_values = [
                list(source.values[r][rect.col0:rect.col1])
                for r in range(rect.row0, rect.row1)
            ]
            relative = f"{name}.shard{index}.cksh"
            header: ShardHeader = write_shard(
                os.path.join(root, relative),
                rows=source.rows,
                cols=source.cols,
                rect=rect,
                stride_floats=plan.stride_floats,
                values=sub_values,
                end_padding=plan.end_padding,
            )
            records.append(
                {
                    "path": relative,
                    "rect": rect.as_tuple(),
                    "stride_floats": header.stride_floats,
                    "end_padding": header.end_padding,
                }
            )

        manifest_tensors[name] = {
            "rows": source.rows,
            "cols": source.cols,
            "shards": records,
        }

    manifest = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "dtype": DTYPE_NAME,
        "tensors": manifest_tensors,
    }
    validate_manifest(manifest)
    manifest_path = os.path.join(root, manifest_name)
    with open(manifest_path, "w", encoding="utf-8") as file_obj:
        json.dump(manifest, file_obj, indent=2, sort_keys=True)
        file_obj.write("\n")
    return os.path.abspath(manifest_path)


def load_manifest(manifest_path: str) -> Dict[str, Any]:
    """Read and validate a manifest; paths inside stay relative to it."""

    try:
        with open(manifest_path, "r", encoding="utf-8") as file_obj:
            manifest = json.load(file_obj)
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {manifest_path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {manifest_path!r} is not valid JSON: {exc}") from exc
    validate_manifest(manifest)
    return manifest


def shard_abs_path(manifest_path: str, relative: str) -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(manifest_path)), relative))


def manifest_tensor_shapes(manifest: Mapping[str, Any]) -> Dict[str, tuple]:
    return {
        name: (int(tensor["rows"]), int(tensor["cols"]))
        for name, tensor in manifest["tensors"].items()
    }


def manifest_shard_records(tensor: Mapping[str, Any]) -> list:
    return list(tensor["shards"])
