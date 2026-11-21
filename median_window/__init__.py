"""Streaming median window operator (closed window, self-timestamp excluded).

Public API
----------
``MedianWindow(window, seed=0)``
    Push-based operator.  ``push(partition, time, value)`` returns a
    :class:`WindowRow`; ``push_many`` / ``push_batches`` accept iterables and
    nested batch iterables; ``finish()`` closes the stream; ``stats()``
    exposes structural churn.

``run_stream(records, window, seed=0)``
    One-shot helper returning ``list[WindowRow]``.

``iter_medians(records, window, seed=0)``
    Lazily yields the median for each record.

``reference_output(records, window)``
    Brute-force reference implementation used by the test suite.

``WindowRow``
    ``row_number`` (1-based global arrival index), ``partition``, ``time``,
    ``value``, ``median``.

Semantics: for a record at time ``t`` report the median of the closed window
``[t - W, t]`` after removing all rows whose time is ``t`` in the same
partition; even counts average the two middle values; an empty window yields
``None`` (SQL NULL).  Partitions reset all state; record order is preserved.
"""

from .treap import OrderedMultiset
from .window import (
    MEDIAN_NULL,
    MedianWindow,
    OrderingError,
    WindowError,
    WindowRow,
    WindowStats,
    iter_medians,
    reference_output,
    run_stream,
)

__all__ = [
    "MEDIAN_NULL",
    "MedianWindow",
    "OrderedMultiset",
    "OrderingError",
    "WindowError",
    "WindowRow",
    "WindowStats",
    "iter_medians",
    "reference_output",
    "run_stream",
]
