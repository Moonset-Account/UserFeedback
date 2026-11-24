"""Loaders for dictionary / JSON pipeline and snapshot specifications.

Pipeline specification
----------------------

.. code-block:: json

    {
      "initial_values": {"x": 0},
      "outputs_of_interest": ["out"],
      "steps": [
        {
          "name": "build",
          "reads": ["x"],
          "deps": [],
          "lock": "gcc",
          "table": [
            {"when": [0], "publish": {"out": 1}}
          ]
        }
      ]
    }

* ``when`` is the frozen input tuple (one entry per entry of ``reads``).
* A read of a name that was never published is represented by the marker
  ``{"$unset": true}`` in specifications, and by the ``UNSET`` sentinel in
  the Python API.
* Publishing several names inside one ``publish`` object happens atomically
  at the step's end event.

Snapshot specification
----------------------

.. code-block:: json

    {
      "statuses": {"build": "running"},
      "frozen_inputs": {"build": [0]},
      "published": {"x": 0},
      "lock_holders": {"gcc": "build"}
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .model import (
    ENDED,
    PENDING,
    RUNNING,
    UNSET,
    Pipeline,
    Snapshot,
    Step,
    build_pipeline,
    freeze,
)

_UNSET_MARKER = "$unset"


def _decode(value: Any) -> Any:
    if isinstance(value, Mapping):
        if len(value) == 1 and _UNSET_MARKER in value:
            return UNSET
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _encode_table(entries: Sequence[Mapping[str, Any]], reads: Sequence[str]) -> Dict[tuple, Mapping[str, Any]]:
    table: Dict[tuple, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or "when" not in entry or "publish" not in entry:
            raise ValidationError("each table entry needs 'when' and 'publish' fields")
        when = entry["when"]
        if not isinstance(when, list):
            raise ValidationError("table entry 'when' must be a list")
        if len(when) != len(reads):
            raise ValidationError(
                f"table entry {entry!r} has {len(when)} input(s), expected {len(reads)}"
            )
        key = tuple(freeze(_decode(v)) for v in when)
        if key in table:
            raise ValidationError(f"duplicate table entry for input {list(key)!r}")
        published = _decode(entry["publish"])
        if not isinstance(published, Mapping):
            raise ValidationError("table entry 'publish' must be an object")
        table[key] = published
    return table


def pipeline_from_dict(spec: Mapping[str, Any]) -> Pipeline:
    """Build a :class:`Pipeline` from a plain dictionary specification."""
    if not isinstance(spec, Mapping):
        raise ValidationError("pipeline specification must be an object")
    raw_steps = spec.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValidationError("'steps' must be a non-empty list")
    steps = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            raise ValidationError("every step must be an object")
        for required in ("name", "reads", "table"):
            if required not in raw:
                raise ValidationError(f"step is missing required field {required!r}")
        reads = raw["reads"]
        if not isinstance(reads, list):
            raise ValidationError(f"step {raw.get('name')!r}: 'reads' must be a list")
        table_raw = raw["table"]
        if not isinstance(table_raw, list):
            raise ValidationError(f"step {raw.get('name')!r}: 'table' must be a list")
        step = Step(
            name=str(raw["name"]),
            reads=tuple(str(r) for r in reads),
            table=_encode_table(table_raw, reads),
            lock=raw.get("lock"),
            deps=tuple(str(d) for d in raw.get("deps", [])),
        )
        steps.append(step)
    initial = _decode(spec.get("initial_values", {}))
    if not isinstance(initial, Mapping):
        raise ValidationError("'initial_values' must be an object")
    outputs = spec.get("outputs_of_interest", [])
    if not isinstance(outputs, list):
        raise ValidationError("'outputs_of_interest' must be a list")
    return build_pipeline(steps, initial, [str(n) for n in outputs])


def pipeline_from_file(path: str | Path) -> Pipeline:
    """Load a pipeline specification from a JSON file."""
    with open(path, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    return pipeline_from_dict(spec)


def snapshot_from_dict(pipeline: Pipeline, spec: Mapping[str, Any]) -> Snapshot:
    """Build a :class:`Snapshot` for ``pipeline`` from a plain dictionary."""
    if not isinstance(spec, Mapping):
        raise ValidationError("snapshot specification must be an object")
    statuses_raw = spec.get("statuses", {})
    if not isinstance(statuses_raw, Mapping):
        raise ValidationError("snapshot 'statuses' must be an object")
    valid = {PENDING, RUNNING, ENDED}
    statuses: Dict[str, str] = {}
    for name, st in statuses_raw.items():
        if st not in valid:
            raise ValidationError(f"snapshot: status {st!r} for {name} is invalid")
        statuses[str(name)] = st
    frozen_raw = spec.get("frozen_inputs", {})
    if not isinstance(frozen_raw, Mapping):
        raise ValidationError("snapshot 'frozen_inputs' must be an object")
    frozen_inputs = {}
    for name, values in frozen_raw.items():
        if not isinstance(values, (list, tuple)):
            values = [values]
        frozen_inputs[str(name)] = tuple(freeze(_decode(v)) for v in values)
    published = _decode(spec.get("published", {}))
    if not isinstance(published, Mapping):
        raise ValidationError("snapshot 'published' must be an object")
    holders_raw = spec.get("lock_holders", {})
    if not isinstance(holders_raw, Mapping):
        raise ValidationError("snapshot 'lock_holders' must be an object")
    return Snapshot(
        pipeline=pipeline,
        statuses=statuses,
        frozen_inputs=frozen_inputs,
        published=published,
        lock_holders={str(k): str(v) for k, v in holders_raw.items()},
    )
