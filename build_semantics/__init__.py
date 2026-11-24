"""Build execution semantics library.

A small pipeline is described as a DAG of steps.  Each step:

* reads named, already-published values from a shared workspace;
* freezes the values it reads when it *starts* (a late-publishing writer can
  never change what a running step saw);
* when it *ends*, publishes several values at once by applying a finite,
  total lookup table given in the problem input to its frozen input tuple;
* may hold one mutually-exclusive executor (a lock) from its start event to
  its end event; two steps using the same executor can never overlap.

The library either proves that every legal interleaving produces the same
final values for the outputs of interest, or returns two replayable
start/end event traces that witness different final values.  Witness traces
are ordered first by the total length left after removing their longest
common prefix (equivalently: the longest possible shared prefix, since all
complete traces of one pipeline have equal length), then by the
lexicographic order of the two traces viewed as a sorted pair.

Resume semantics: a post-failure breakpoint snapshot (frozen inputs of
running steps, lock holders, published values and the DAG position of every
step) is a first-class analysis root, so a trace that read a stale value and
finishes late cannot be missed.
"""

from .model import (
    UNSET,
    AnalysisResult,
    Pipeline,
    Snapshot,
    Step,
    analyze,
    build_pipeline,
    analyze_snapshot,
    replay,
    replay_snapshot,
    event_strings,
)
from .loader import pipeline_from_dict, pipeline_from_file, snapshot_from_dict
from .errors import SemanticsError, ValidationError, LookupTableError, TraceError

__all__ = [
    "UNSET",
    "AnalysisResult",
    "Pipeline",
    "Snapshot",
    "Step",
    "analyze",
    "build_pipeline",
    "analyze_snapshot",
    "replay",
    "replay_snapshot",
    "event_strings",
    "pipeline_from_dict",
    "pipeline_from_file",
    "snapshot_from_dict",
    "SemanticsError",
    "ValidationError",
    "LookupTableError",
    "TraceError",
]

__version__ = "1.0.0"
