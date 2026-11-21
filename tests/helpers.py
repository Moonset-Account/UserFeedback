"""Small builders shared by the acceptance tests.

Everything is in-memory and synthetic: no network, no external processes.
"""

from __future__ import annotations

from shufflesched import ShuffleScheduler


def diamond_scheduler() -> ShuffleScheduler:
    """Source S -> narrow M -> shuffle R, with M' as an independent source.

    Layout (classic diamond / stage fan-in)::

        S (source)  ->  M (narrow)  --\\
                                        R (shuffle, join of M and S2)
        S2 (source) ------------------/

    ``M`` is a narrow child of ``S`` and ``R`` reads both ``M`` and ``S2``, so a
    loss of ``S``'s block reaches ``R`` through ``M`` (a multi-level path) while
    ``S2`` remains an untouched sibling branch.
    """
    sched = ShuffleScheduler()
    sched.add_partition("S")
    sched.add_partition("S2")
    sched.add_partition("M", ["S"], kind="narrow")
    sched.add_partition("R", ["M", "S2"], kind="shuffle")
    sched.seal()
    return sched


def wide_diamond_scheduler() -> ShuffleScheduler:
    """A true diamond: two independent sources merge into one join.

    ::

        A (source) -> B (narrow) --\\
                                     J (shuffle)
        C (source) -> D (narrow) --/

    A lost block in ``B`` must recompute ``B`` and ``J`` only.  A lost block in
    ``A`` must recompute ``A``, ``B`` and ``J`` but leave ``C``/``D`` untouched.
    """
    sched = ShuffleScheduler()
    sched.add_partition("A")
    sched.add_partition("C")
    sched.add_partition("B", ["A"], kind="narrow")
    sched.add_partition("D", ["C"], kind="narrow")
    sched.add_partition("J", ["B", "D"], kind="shuffle")
    sched.seal()
    return sched


def materialise(sched: ShuffleScheduler, plan: dict[str, tuple[str, list[str]]]) -> None:
    """Feed source blocks in ``plan``: pid -> (executor, [attempt ids, in order])."""
    for pid, (executor, attempt_ids) in plan.items():
        for attempt_id in attempt_ids:
            result = sched.provide_source(pid, attempt_id, executor, payload=attempt_id)
            assert result.accepted, (pid, attempt_id, result)
