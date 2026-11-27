"""Event-driven inter-process channel kernel.

The kernel implements a small, deterministic model of message passing with
support for both *buffered* and *unbuffered* channels, plus *selection*: a
caller may register one action carrying several candidate branches, at most one
of which is ever completed.

Everything that changes state is driven by an **event list**.  Events are
consumed strictly in the order they were queued, so when a cancellation /
timeout and a "became ready" observation land on the same beat, the event list
decides which one wins.

Semantics
=========

Buffered channels (``capacity > 0``)
    A send writes into a FIFO queue when there is room; a receive takes the
    oldest value from the queue when it is non-empty.  Transfers never bypass
    the queue, so FIFO order is always preserved.  A send that finds the queue
    full, or a receive that finds it empty, stays ``pending`` and is retried on
    subsequent matching rounds.

Unbuffered channels (``capacity == 0``)
    There is no storage.  A transfer requires one live ``send`` and one live
    ``receive`` from two **different callers**, both present at commit time.
    The value moves directly from sender to receiver.

Selection
    Two branches that belong to the same action (the same choice) can never be
    paired with one another.  When one branch commits, the action is done and
    all of its sibling branches are removed from candidacy at the same time.

Tie-breaking
    Ties are resolved by action registration order (lower ``seq`` first) and,
    within one action, by branch index (lower index first).  The same two keys
    order the peers considered during a rendezvous.

Cancellation / timeout
    ``cancel`` and ``timeout`` withdraw a live action, giving it a terminal
    state and no value.  Withdrawing an action that already finished is a no-op,
    so a late cancellation can never take back or duplicate a value that has
    already been delivered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

__all__ = [
    "Branch",
    "Channel",
    "Kernel",
    "ActionState",
    "PairRecord",
    "PENDING",
    "SENT",
    "RECEIVED",
    "CANCELLED",
    "TIMED_OUT",
    "COMMITTED",
    "WITHDRAWN",
]


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

PENDING = "pending"        # live candidate, not yet resolved
SENT = "sent"              # a send action completed; ``value`` is what it sent
RECEIVED = "received"      # a receive action completed; ``value`` is what it got
CANCELLED = "cancelled"    # withdrawn by an explicit cancel event
TIMED_OUT = "timed_out"    # withdrawn by a timeout event

COMMITTED = "committed"    # this branch is the one that carried the transfer
WITHDRAWN = "withdrawn"    # sibling of a committed branch, or all of a withdrawn action

_SEND = "send"
_RECV = "receive"
_TERMINAL = frozenset({SENT, RECEIVED, CANCELLED, TIMED_OUT})


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Branch:
    """One alternative of an action.

    ``kind`` is ``"send"`` or ``"receive"``.  ``channel`` is the target
    ``Channel``.  ``value`` is the payload of a send and is ignored by a
    receive.
    """

    kind: str
    channel: "Channel"
    value: Any = None

    def __post_init__(self) -> None:
        if self.kind not in (_SEND, _RECV):
            raise ValueError(
                f"branch kind must be {_SEND!r} or {_RECV!r}, got {self.kind!r}"
            )
        if not isinstance(self.channel, Channel):
            raise TypeError("branch.channel must be a Channel instance")


@dataclass(frozen=True)
class ActionState:
    """Immutable snapshot of one action's outcome.

    ``value`` holds the transferred value for a completed send/receive and is
    ``None`` for pending, cancelled and timed-out actions.  ``branch`` is the
    branch index that committed (``None`` otherwise).  ``peer``/``peer_branch``
    identify the action/branch on the other side of a rendezvous, and are
    ``None`` for buffered transfers (which are decoupled) and for withdrawn
    actions.
    """

    seq: int
    caller: str
    state: str
    value: Any = None
    branch: Optional[int] = None
    peer: Optional[int] = None
    peer_branch: Optional[int] = None

    @property
    def finished(self) -> bool:
        return self.state in _TERMINAL


@dataclass(frozen=True)
class PairRecord:
    """A completed unbuffered rendezvous, useful for auditing."""

    send_seq: int
    send_branch: int
    recv_seq: int
    recv_branch: int
    value: Any


class _Action:
    """Mutable bookkeeping for a registered action."""

    __slots__ = ("seq", "caller", "branches", "state", "value", "branch", "peer", "peer_branch")

    def __init__(self, seq: int, caller: str, branches: Iterable[Branch]) -> None:
        self.seq = seq
        self.caller = caller
        self.branches = list(branches)
        self.state = PENDING
        self.value: Any = None
        self.branch: Optional[int] = None
        self.peer: Optional[int] = None
        self.peer_branch: Optional[int] = None

    @property
    def live(self) -> bool:
        return self.state == PENDING

    def snapshot(self) -> ActionState:
        return ActionState(
            seq=self.seq,
            caller=self.caller,
            state=self.state,
            value=self.value,
            branch=self.branch,
            peer=self.peer,
            peer_branch=self.peer_branch,
        )

    def branch_outcomes(self) -> tuple[str, ...]:
        """Per-branch status for this action.

        Each entry is one of ``PENDING`` (still a candidate), ``COMMITTED``
        (this branch carried the transfer) or ``WITHDRAWN`` (the action was
        resolved through a sibling branch, or the action was cancelled / timed
        out).  A committed action therefore has exactly one ``COMMITTED``
        entry and every other branch is ``WITHDRAWN``.
        """
        out: list[str] = []
        for index in range(len(self.branches)):
            if self.state == PENDING:
                out.append(PENDING)
            elif self.branch == index:
                out.append(COMMITTED)
            else:
                out.append(WITHDRAWN)
        return tuple(out)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class Channel:
    """A buffered (``capacity > 0``) or unbuffered (``capacity == 0``) channel."""

    __slots__ = ("name", "capacity", "_queue")

    def __init__(self, name: str, capacity: int = 0) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise TypeError("capacity must be an int")
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        self.name = name
        self.capacity = capacity
        self._queue: list[Any] = []

    @property
    def buffered(self) -> bool:
        """True when the channel can hold values in a FIFO queue."""
        return self.capacity > 0

    def __len__(self) -> int:
        return len(self._queue)

    def snapshot(self) -> tuple[Any, ...]:
        """Copy of the queued values, oldest first."""
        return tuple(self._queue)

    def seed(self, *values: Any) -> None:
        """Place values directly into the queue, bypassing the kernel.

        This exists purely to build fixture states (e.g. a full or pre-filled
        channel) without having to register throw-away actions.  It still
        respects ``capacity`` and appends in argument order.
        """
        if len(self._queue) + len(values) > self.capacity:
            raise ValueError("seeding would exceed channel capacity")
        self._queue.extend(values)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Channel({self.name!r}, capacity={self.capacity}, "
            f"queue={list(self._queue)!r})"
        )

    # Internal queue primitives.  Callers never manipulate the queue directly.
    def _push(self, value: Any) -> None:
        if len(self._queue) >= self.capacity:
            raise RuntimeError("channel queue is full")
        self._queue.append(value)

    def _pop(self) -> Any:
        if not self._queue:
            raise RuntimeError("channel queue is empty")
        return self._queue.pop(0)


# ---------------------------------------------------------------------------
# Kernel
# ---------------------------------------------------------------------------


class Kernel:
    """Event-list driven channel kernel.

    Typical use::

        ch = Channel("jobs")
        k = Kernel()
        recv_seq = k.register("worker", [("receive", ch)])
        send_seq = k.register("producer", [("send", ch, "payload")])
        k.event("ready")
        outcome = k.run()
    """

    def __init__(self) -> None:
        self._order: list[int] = []          # registration order
        self._actions: dict[int, _Action] = {}
        self._counter = 0
        self._events: list[tuple[str, Optional[int]]] = []
        self._pairs: list[PairRecord] = []
        self._timeline: list[ActionState] = []

    # -- registration -----------------------------------------------------

    def register(self, caller: str, branches: Iterable) -> int:
        """Register an action and return its sequence number.

        ``branches`` is a non-empty iterable of ``Branch`` objects or of
        ``(kind, channel[, value])`` tuples.  Branch order is significant: the
        lower index wins when several branches of the same action are ready.
        """
        prepared: list[Branch] = []
        for item in branches:
            if isinstance(item, Branch):
                prepared.append(item)
            else:
                parts = list(item)
                if len(parts) not in (2, 3):
                    raise ValueError(
                        "branch tuples must be (kind, channel) or (kind, channel, value)"
                    )
                kind, channel = parts[0], parts[1]
                value = parts[2] if len(parts) == 3 else None
                prepared.append(Branch(kind, channel, value))
        if not prepared:
            raise ValueError("an action needs at least one branch")

        seq = self._counter
        self._counter += 1
        self._actions[seq] = _Action(seq, caller, prepared)
        self._order.append(seq)
        return seq

    # -- event list -------------------------------------------------------

    def event(self, kind: str, seq: Optional[int] = None) -> "Kernel":
        """Queue one event.  Events run in the order queued.

        Supported kinds:

        ``"ready"``
            Run a matching round.  ``seq`` is accepted for readability but the
            round itself considers every live action (in registration order),
            so it cannot reorder ties.
        ``"cancel"``
            Withdraw action ``seq`` into ``CANCELLED``.
        ``"timeout"``
            Withdraw action ``seq`` into ``TIMED_OUT``.
        """
        if kind not in ("ready", "cancel", "timeout"):
            raise ValueError(f"unknown event kind {kind!r}")
        if kind in ("cancel", "timeout") and seq is None:
            raise ValueError(f"{kind!r} event requires a seq")
        self._events.append((kind, seq))
        return self

    def ready(self, seq: Optional[int] = None) -> "Kernel":
        return self.event("ready", seq)

    def cancel(self, seq: int) -> "Kernel":
        return self.event("cancel", seq)

    def timeout(self, seq: int) -> "Kernel":
        return self.event("timeout", seq)

    # -- driving ----------------------------------------------------------

    def has_events(self) -> bool:
        """True while any queued event remains unprocessed."""
        return bool(self._events)

    def step(self) -> bool:
        """Process exactly one queued event.  Returns False when the list is empty.

        This is the primitive that makes the event-list order observable: the
        caller can inspect state between two events (e.g. after a ``ready`` but
        before a ``cancel`` on the same beat).
        """
        if not self._events:
            return False
        kind, seq = self._events.pop(0)
        if kind == "ready":
            self._match_round()
        else:                       # cancel / timeout
            self._withdraw(seq, CANCELLED if kind == "cancel" else TIMED_OUT)
        return True

    def run(self, settle: bool = True) -> list[ActionState]:
        """Consume the event list in order and return per-action outcomes.

        With ``settle=True`` (the default) a final matching round runs after the
        list drains, so anything that became possible settles.  Pass
        ``settle=False`` to honour the event list exactly: then only ``ready``
        events commit anything, and actions left over after the last event stay
        ``pending``.

        Withdrawals processed earlier in the list still take precedence in
        either mode, because a withdrawn action is no longer a candidate.
        """
        while self.step():
            pass
        if settle:
            self._match_round()
        return self.snapshots()

    # -- inspection -------------------------------------------------------

    def snapshots(self) -> list[ActionState]:
        """All actions in registration order."""
        return [self._actions[s].snapshot() for s in self._order]

    def results(self) -> dict[int, ActionState]:
        """Outcome per action sequence number."""
        return {s: self._actions[s].snapshot() for s in self._order}

    def state_of(self, seq: int) -> ActionState:
        return self._actions[seq].snapshot()

    def branches_of(self, seq: int) -> tuple[str, ...]:
        """Per-branch outcome for action ``seq`` (see ``_Action.branch_outcomes``)."""
        return self._actions[seq].branch_outcomes()

    def pair_log(self) -> list[PairRecord]:
        """Completed unbuffered rendezvous, in completion order."""
        return list(self._pairs)

    def timeline(self) -> list[ActionState]:
        """Every completion / withdrawal, in the order it happened."""
        return list(self._timeline)

    def pending(self) -> list[int]:
        """Sequence numbers of actions still live."""
        return [s for s in self._order if self._actions[s].live]

    # -- matching engine --------------------------------------------------

    def _live_in_order(self) -> list[_Action]:
        return [self._actions[s] for s in self._order if self._actions[s].live]

    def _match_round(self) -> None:
        """Commit pairs until nothing more can be committed."""
        progressed = True
        while progressed:
            progressed = False
            for action in self._live_in_order():
                if self._try_action(action):
                    progressed = True
                    break

    def _try_action(self, action: _Action) -> bool:
        for index, branch in enumerate(action.branches):
            if self._attempt(action, index, branch):
                return True
        return False

    def _attempt(self, action: _Action, index: int, branch: Branch) -> bool:
        channel = branch.channel
        if channel.buffered:
            if branch.kind == _RECV and len(channel) > 0:
                value = channel._pop()
                self._finish_solo(action, index, RECEIVED, value)
                return True
            if branch.kind == _SEND and len(channel) < channel.capacity:
                channel._push(branch.value)
                self._finish_solo(action, index, SENT, branch.value)
                return True
            return False
        return self._rendezvous(action, index, branch)

    def _rendezvous(self, action: _Action, index: int, branch: Branch) -> bool:
        want = _SEND if branch.kind == _RECV else _RECV
        for other in self._live_in_order():
            if other.seq == action.seq:
                continue                      # a choice never matches itself
            if other.caller == action.caller:
                continue                      # unbuffered needs different callers
            for other_index, other_branch in enumerate(other.branches):
                if other_branch.channel is not branch.channel:
                    continue
                if other_branch.kind != want:
                    continue
                if branch.kind == _SEND:
                    transferred = branch.value
                else:
                    transferred = other_branch.value
                self._commit_pair(action, index, other, other_index, transferred)
                return True
        return False

    # -- state transitions ------------------------------------------------

    def _finish_solo(self, action: _Action, index: int, state: str, value: Any) -> None:
        action.state = state
        action.value = value
        action.branch = index
        action.peer = None
        action.peer_branch = None
        self._timeline.append(action.snapshot())

    def _commit_pair(self, a: _Action, a_index: int, b: _Action, b_index: int, value: Any) -> None:
        a.state = SENT if a.branches[a_index].kind == _SEND else RECEIVED
        a.value = value
        a.branch = a_index
        a.peer = b.seq
        a.peer_branch = b_index

        b.state = SENT if b.branches[b_index].kind == _SEND else RECEIVED
        b.value = value
        b.branch = b_index
        b.peer = a.seq
        b.peer_branch = a_index

        if a.state == SENT:
            send_seq, send_branch, recv_seq, recv_branch = a.seq, a_index, b.seq, b_index
        else:
            send_seq, send_branch, recv_seq, recv_branch = b.seq, b_index, a.seq, a_index
        self._pairs.append(PairRecord(send_seq, send_branch, recv_seq, recv_branch, value))
        self._timeline.append(a.snapshot())
        self._timeline.append(b.snapshot())

    def _withdraw(self, seq: Optional[int], state: str) -> None:
        if seq is None:
            return
        action = self._actions.get(seq)
        if action is None or not action.live:
            return                            # late cancel: keep the value as-is
        action.state = state
        action.value = None
        action.branch = None
        action.peer = None
        action.peer_branch = None
        self._timeline.append(action.snapshot())
