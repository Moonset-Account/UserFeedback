"""Happens-before order reconstruction.

Two kinds of ordering evidence are accepted, matching the requirements:

1. Thread-internal order: events sharing a ``thread`` are ordered by ``seq``.
2. Explicit synchronization edges:
   - a ``SYNC`` wait at sync point X follows the matching ``SYNC`` signal at X;
   - an event's ``depends_on`` list names events it directly follows.

Nothing else is assumed to establish order. In particular, arrival order in
the input stream is never consulted, so shuffling the records cannot change a
verdict.
"""

from collections import deque

from .events import Event, SYNC, SYNC_SIGNAL, SYNC_WAIT


class OrderGraph:
    def __init__(self, events: list[Event]):
        self.events = events
        self.by_id: dict[str, Event] = {e.event_id: e for e in events}
        self._pred: dict[str, list[tuple[str, str]]] = {
            e.event_id: [] for e in events
        }
        self._succ: dict[str, list[str]] = {e.event_id: [] for e in events}
        self.dangling_edges: list[tuple[Event, str, str]] = []
        self.thread_seq_conflicts: list[tuple[str, int, list[str]]] = []
        self.cycles: list[list[str]] = []
        self._reach_cache: dict[tuple[str, str], bool] = {}
        self._path_cache: dict[tuple[str, str], list[str] | None] = {}
        self._build()

    # ------------------------------------------------------------------ build
    def _add_edge(self, src_id: str, dst: Event, label: str) -> None:
        src = self.by_id.get(src_id)
        if src is None:
            # The predecessor was never observed: the synchronization record
            # that would establish the order is missing.
            self.dangling_edges.append((dst, src_id, label))
            return
        if src_id == dst.event_id:
            return
        self._pred[dst.event_id].append((src_id, label))
        self._succ[src_id].append(dst.event_id)

    def _build(self) -> None:
        # (1) thread-internal seq order
        per_thread: dict[str, list[Event]] = {}
        for e in self.events:
            if e.thread is not None and e.seq is not None:
                per_thread.setdefault(e.thread, []).append(e)

        for thread, group in per_thread.items():
            ordered = sorted(group, key=lambda e: e.seq)
            seen_seq: dict[int, str] = {}
            for e in ordered:
                if e.seq in seen_seq:
                    dup = [
                        x.event_id
                        for x in ordered
                        if x.seq == e.seq
                    ]
                    if not any(
                        c[0] == thread and c[1] == e.seq
                        for c in self.thread_seq_conflicts
                    ):
                        self.thread_seq_conflicts.append((thread, e.seq, dup))
                seen_seq[e.seq] = e.event_id
            for prev, cur in zip(ordered, ordered[1:]):
                if prev.seq == cur.seq:
                    continue  # conflict recorded above; no order asserted
                self._add_edge(
                    prev.event_id, cur, f"thread:{thread} seq"
                )

        # (2a) SYNC signal -> matching wait
        signals: dict[str, Event] = {}
        waits: list[Event] = []
        for e in self.events:
            if e.type != SYNC or not e.sync_id:
                continue
            if e.sync_op == SYNC_SIGNAL:
                signals.setdefault(e.sync_id, e)
            elif e.sync_op == SYNC_WAIT:
                waits.append(e)
        for w in waits:
            s = signals.get(w.sync_id)
            if s is None:
                self.dangling_edges.append(
                    (w, f"signal:{w.sync_id}", "sync")
                )
            else:
                self._add_edge(s.event_id, w, f"sync:{w.sync_id}")

        # (2b) explicit depends_on edges
        for e in self.events:
            for dep in e.depends_on:
                self._add_edge(dep, e, "depends_on")

        self._detect_cycles()

    def _detect_cycles(self) -> None:
        color: dict[str, int] = {e.event_id: 0 for e in self.events}
        stack: list[str] = []

        def visit(node: str) -> None:
            color[node] = 1
            stack.append(node)
            for nxt in self._succ[node]:
                if color[nxt] == 0:
                    visit(nxt)
                elif color[nxt] == 1:
                    start = stack.index(nxt)
                    self.cycles.append(stack[start:] + [nxt])
            stack.pop()
            color[node] = 2

        for e in self.events:
            if color[e.event_id] == 0:
                visit(e.event_id)

    # ---------------------------------------------------------------- queries
    def hb(self, before_id: str, after_id: str) -> bool:
        """True when ``before_id`` is proven to happen-before ``after_id``."""
        if before_id not in self.by_id or after_id not in self.by_id:
            return False
        key = (before_id, after_id)
        cached = self._reach_cache.get(key)
        if cached is not None:
            return cached
        found = self.path(before_id, after_id) is not None
        self._reach_cache[key] = found
        return found

    def path(self, before_id: str, after_id: str) -> list[str] | None:
        """Shortest chain of event ids proving the happens-before relation."""
        if before_id not in self.by_id or after_id not in self.by_id:
            return None
        key = (before_id, after_id)
        if key in self._path_cache:
            return self._path_cache[key]
        if before_id == after_id:
            result = [before_id]
        else:
            result = self._bfs(before_id, after_id)
        self._path_cache[key] = result
        return result

    def _bfs(self, src: str, dst: str) -> list[str] | None:
        prev: dict[str, str] = {src: src}
        queue: deque[str] = deque([src])
        while queue:
            node = queue.popleft()
            for nxt in self._succ[node]:
                if nxt in prev:
                    continue
                prev[nxt] = node
                if nxt == dst:
                    chain = [dst]
                    while chain[-1] != src:
                        chain.append(prev[chain[-1]])
                    chain.reverse()
                    return chain
                queue.append(nxt)
        return None

    def edge_label(self, src_id: str, dst_id: str) -> str:
        for pred_id, label in self._pred[dst_id]:
            if pred_id == src_id:
                return label
        return "?"

    def explain(self, before_id: str, after_id: str) -> list[str]:
        """Render a happens-before path as labeled evidence lines."""
        chain = self.path(before_id, after_id)
        if chain is None:
            return []
        lines = [chain[0]]
        for a, b in zip(chain, chain[1:]):
            lines.append(f"  --[{self.edge_label(a, b)}]--> {b}")
        return lines

    def dangling_for(self, event: Event) -> list[tuple[str, str]]:
        return [
            (ref, label)
            for ev, ref, label in self.dangling_edges
            if ev.event_id == event.event_id
        ]

    def topological_replay(self) -> list[Event]:
        """Deterministic total order consistent with all proven edges.

        Ties break on (thread, seq, event_id) so the replay order depends only
        on the log content, never on arrival order. Events trapped in cycles
        are skipped (the cycle is reported separately as a finding).
        """
        indegree = {
            eid: len(preds) for eid, preds in self._pred.items()
        }
        ready = [eid for eid, d in indegree.items() if d == 0]
        order: list[Event] = []
        import heapq

        def rank(eid: str) -> tuple:
            e = self.by_id[eid]
            return (
                e.thread is None,
                e.thread or "",
                e.seq if e.seq is not None else 1 << 30,
                e.event_id,
            )

        heap = [(rank(eid), eid) for eid in ready]
        heapq.heapify(heap)
        done: set[str] = set()
        while heap:
            _, eid = heapq.heappop(heap)
            if eid in done:
                continue
            done.add(eid)
            order.append(self.by_id[eid])
            for nxt in self._succ[eid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    heapq.heappush(heap, (rank(nxt), nxt))
        return order
