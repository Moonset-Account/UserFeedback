"""Core data model: peer FSMs, message mapping and buffer configuration.

A peer is described by a finite state machine whose transitions are labeled
with either a SEND or a RECEIVE of a message.  The peer transition graph must
be acyclic: the synthesis problem only considers acyclic calls, so a cycle in
either peer graph is rejected at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple


class Act(str, Enum):
    """Label carried by a peer transition."""

    SEND = "send"
    RECV = "recv"


class Direction(str, Enum):
    """Direction of a message flow.

    A2B messages originate at peer A and are destined for peer B (and B2A is
    the reverse).  Pending/converted FIFOs are kept per direction.
    """

    A2B = "a2b"
    B2A = "b2a"


@dataclass(frozen=True)
class PeerTransition:
    """A single labeled edge of a peer FSM.

    ``source`` -> ``target`` while performing ``act`` on ``message``.
    """

    source: str
    target: str
    act: Act
    message: str


@dataclass(frozen=True)
class PeerFSM:
    """A finite state machine for one peer.

    ``states`` is the explicit set of declared state names.  Every
    transition endpoint, the initial state and every final state must belong
    to it, and the directed state graph must be acyclic.  ``alphabet`` is the
    full set of messages the peer can ever send or receive.
    """

    name: str
    initial: str
    finals: Tuple[str, ...]
    transitions: Tuple[PeerTransition, ...]
    alphabet: Tuple[str, ...]
    states: Tuple[str, ...] = ()
    outgoing: dict = field(default_factory=dict, compare=False)
    topo_index: dict = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        declared = set(self.states) if self.states else self._collect_states()
        if not self.states:
            object.__setattr__(self, "states", tuple(sorted(declared)))
        self._validate(declared)
        outgoing: dict = {s: [] for s in declared}
        for tr in self.transitions:
            outgoing[tr.source].append(tr)
        # Deterministic ordering keeps every derived artefact reproducible.
        for s in outgoing:
            outgoing[s].sort(key=lambda t: (t.act.value, t.message, t.target))
        object.__setattr__(self, "outgoing", outgoing)
        object.__setattr__(self, "topo_index", self._topological_order(declared))

    def _collect_states(self) -> set:
        states = {self.initial, *self.finals}
        for tr in self.transitions:
            states.add(tr.source)
            states.add(tr.target)
        return states

    def _validate(self, states: set) -> None:
        if self.initial not in states:
            raise ValueError(f"{self.name}: initial state {self.initial!r} undeclared")
        for final in self.finals:
            if final not in states:
                raise ValueError(f"{self.name}: final state {final!r} undeclared")
        for tr in self.transitions:
            if tr.source not in states or tr.target not in states:
                raise ValueError(
                    f"{self.name}: transition {tr} references an undeclared state"
                )
            if not isinstance(tr.act, Act):
                raise ValueError(f"{self.name}: transition {tr} has a bad action")
            if tr.message not in self.alphabet:
                raise ValueError(
                    f"{self.name}: message {tr.message!r} is not in the alphabet"
                )
        if self._has_cycle(states):
            raise ValueError(
                f"{self.name}: peer state transition graph contains a cycle; "
                "only acyclic calls are supported"
            )

    def _has_cycle(self, states: set) -> bool:
        indegree = {s: 0 for s in states}
        adjacency: dict = {s: [] for s in states}
        for tr in self.transitions:
            adjacency[tr.source].append(tr.target)
            indegree[tr.target] += 1
        ready = [s for s in sorted(states) if indegree[s] == 0]
        seen = 0
        while ready:
            node = ready.pop()
            seen += 1
            for nxt in adjacency[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        return seen != len(states)

    def _topological_order(self, states: set) -> dict:
        indegree = {s: 0 for s in states}
        adjacency: dict = {s: [] for s in states}
        for tr in self.transitions:
            adjacency[tr.source].append(tr.target)
            indegree[tr.target] += 1
        ready = [s for s in sorted(states) if indegree[s] == 0]
        order: dict = {}
        index = 0
        while ready:
            node = ready.pop()
            order[node] = index
            index += 1
            for nxt in sorted(adjacency[node]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        return order

    def is_final(self, state: str) -> bool:
        return state in self.finals


@dataclass(frozen=True)
class MessageMapping:
    """Accepted message translation table.

    ``a2b`` maps messages sent by A into messages received by B, and ``b2a``
    maps messages sent by B into messages received by A.  Every source
    message must belong to the sender alphabet and every image to the
    receiver alphabet.
    """

    a2b: dict
    b2a: dict

    def direction_map(self, direction: Direction) -> dict:
        if direction is Direction.A2B:
            return self.a2b
        return self.b2a

    def image(self, direction: Direction, message: str):
        return self.direction_map(direction).get(message)


@dataclass(frozen=True)
class BufferConfig:
    """Capacities of the four bounded FIFOs.

    Each capacity is between 0 and 2 (each FIFO holds at most two messages).
    """

    pending_a2b: int
    converted_a2b: int
    pending_b2a: int
    converted_b2a: int
    MAX: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("pending_a2b", self.pending_a2b),
            ("converted_a2b", self.converted_a2b),
            ("pending_b2a", self.pending_b2a),
            ("converted_b2a", self.converted_b2a),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            if not 0 <= value <= self.MAX:
                raise ValueError(
                    f"{name}={value} violates the 0..{self.MAX} capacity bound"
                )

    def pending_cap(self, direction: Direction) -> int:
        return self.pending_a2b if direction is Direction.A2B else self.pending_b2a

    def converted_cap(self, direction: Direction) -> int:
        return (
            self.converted_a2b
            if direction is Direction.A2B
            else self.converted_b2a
        )

    def total_capacity(self) -> int:
        return (
            self.pending_a2b
            + self.converted_a2b
            + self.pending_b2a
            + self.converted_b2a
        )

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (
            self.pending_a2b,
            self.converted_a2b,
            self.pending_b2a,
            self.converted_b2a,
        )


@dataclass(frozen=True)
class ProtocolPair:
    """The two peer FSMs together with the accepted message mapping."""

    peer_a: PeerFSM
    peer_b: PeerFSM
    mapping: MessageMapping

    def __post_init__(self) -> None:
        self._validate_mapping(Direction.A2B, self.peer_a, self.peer_b)
        self._validate_mapping(Direction.B2A, self.peer_b, self.peer_a)

    def _validate_mapping(
        self, direction: Direction, sender: PeerFSM, receiver: PeerFSM
    ) -> None:
        table = self.mapping.direction_map(direction)
        for source, image in table.items():
            if source not in sender.alphabet:
                raise ValueError(
                    f"mapping {direction.value}: source {source!r} is not in "
                    f"{sender.name}'s alphabet"
                )
            if image not in receiver.alphabet:
                raise ValueError(
                    f"mapping {direction.value}: image {image!r} is not in "
                    f"{receiver.name}'s alphabet"
                )
