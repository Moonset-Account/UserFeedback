"""Reference (non-incremental) reachability used to validate the operator."""

from __future__ import annotations

from collections import deque
from typing import Hashable, Iterable


def full_reachable(seeds: Iterable[Hashable], edges: Iterable[tuple]) -> set:
    out: dict = {}
    for u, v in edges:
        out.setdefault(u, set()).add(v)
    seen = set(seeds)
    queue = deque(seen)
    while queue:
        u = queue.popleft()
        for v in out.get(u, ()):  # noqa: SIM118 - out values are sets
            if v not in seen:
                seen.add(v)
                queue.append(v)
    return seen
