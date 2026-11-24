"""The adapter/environment game: states, moves, rounds and rank fixpoint.

Game model (see README for the rationale of the turn semantics)
----------------------------------------------------------------
A round first lets the *environment* pick one executable peer action, or an
explicit handoff (``PASS``).  ``PASS`` is only an admissible environment
choice when no peer action is currently enabled -- the statement requires the
environment to hand off in that situation.  Afterwards the *adapter* either
converts one head message of a pending FIFO through the mapping into the
matching converted FIFO, or waits.

A round in which neither side changes state is a failure.  Such a round can
only happen when the environment was forced to ``PASS`` and the adapter has
no enabled conversion either: that is the global deadlock/stall leaf.  When
the environment still has peer moves, ``PASS + WAIT`` never occurs because
``PASS`` is not an admissible choice at that state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import Act, BufferConfig, Direction, ProtocolPair

INF = 10**9


@dataclass(frozen=True)
class PeerMove:
    """An executable peer action chosen by the environment."""

    peer: str  # "a" or "b"
    source: str
    target: str
    act: Act
    message: str


@dataclass(frozen=True)
class Pass:
    """Explicit environment handoff."""


PASS = Pass()


@dataclass(frozen=True)
class Wait:
    """Adapter explicitly does nothing this round."""


WAIT = Wait()

EnvAction = object
AdapterAction = object


@dataclass(frozen=True)
class Convert:
    """Adapter converts the head message of a pending FIFO."""

    direction: Direction
    source_message: str
    destination_message: str


@dataclass(frozen=True)
class GameState:
    """Full observable state of the game.

    The four queues are FIFO tuples: pending/converted for each direction.
    """

    state_a: str
    state_b: str
    pending_a2b: Tuple[str, ...]
    converted_a2b: Tuple[str, ...]
    pending_b2a: Tuple[str, ...]
    converted_b2a: Tuple[str, ...]

    @staticmethod
    def initial(proto: ProtocolPair) -> "GameState":
        return GameState(
            proto.peer_a.initial,
            proto.peer_b.initial,
            (),
            (),
            (),
            (),
        )

    def is_goal(self, proto: ProtocolPair) -> bool:
        if not proto.peer_a.is_final(self.state_a):
            return False
        if not proto.peer_b.is_final(self.state_b):
            return False
        return not (
            self.pending_a2b
            or self.converted_a2b
            or self.pending_b2a
            or self.converted_b2a
        )

    # --- queue helpers -------------------------------------------------

    def pending(self, direction: Direction) -> Tuple[str, ...]:
        return self.pending_a2b if direction is Direction.A2B else self.pending_b2a

    def converted(self, direction: Direction) -> Tuple[str, ...]:
        return (
            self.converted_a2b
            if direction is Direction.A2B
            else self.converted_b2a
        )

    def _with_queues(
        self,
        pending_a2b,
        converted_a2b,
        pending_b2a,
        converted_b2a,
        state_a=None,
        state_b=None,
    ) -> "GameState":
        return GameState(
            self.state_a if state_a is None else state_a,
            self.state_b if state_b is None else state_b,
            tuple(pending_a2b),
            tuple(converted_a2b),
            tuple(pending_b2a),
            tuple(converted_b2a),
        )


# ---------------------------------------------------------------------------
# Move generation
# ---------------------------------------------------------------------------


def _send_direction(peer: str) -> Direction:
    return Direction.A2B if peer == "a" else Direction.B2A


def _recv_direction(peer: str) -> Direction:
    # A receives messages flowing B2A; B receives messages flowing A2B.
    return Direction.B2A if peer == "a" else Direction.A2B


def enabled_moves(
    proto: ProtocolPair, config: BufferConfig, state: GameState
) -> List[PeerMove]:
    """Return every peer action executable in ``state``.

    A SEND is executable when the corresponding pending FIFO still has room;
    a RECV is executable when the converted FIFO's head is exactly the
    message the transition waits for.
    """

    moves: List[PeerMove] = []
    for peer, fsm, current in (
        ("a", proto.peer_a, state.state_a),
        ("b", proto.peer_b, state.state_b),
    ):
        for tr in fsm.outgoing.get(current, ()):
            if tr.act is Act.SEND:
                direction = _send_direction(peer)
                if len(state.pending(direction)) < config.pending_cap(direction):
                    moves.append(
                        PeerMove(peer, tr.source, tr.target, tr.act, tr.message)
                    )
            else:
                direction = _recv_direction(peer)
                queue = state.converted(direction)
                if queue and queue[0] == tr.message:
                    moves.append(
                        PeerMove(peer, tr.source, tr.target, tr.act, tr.message)
                    )
    moves.sort(key=lambda m: (m.peer, m.act.value, m.message, m.target))
    return moves


def available_conversions(
    proto: ProtocolPair, config: BufferConfig, state: GameState
) -> List[Convert]:
    """Return every adapter conversion enabled in ``state``.

    A conversion is enabled when a pending FIFO is non-empty, its head has a
    mapping entry and the destination converted FIFO has room.
    """

    out: List[Convert] = []
    for direction in (Direction.A2B, Direction.B2A):
        pending = state.pending(direction)
        if not pending:
            continue
        head = pending[0]
        image = proto.mapping.image(direction, head)
        if image is None:
            continue
        if len(state.converted(direction)) < config.converted_cap(direction):
            out.append(Convert(direction, head, image))
    return out


def admissible_env_choices(
    proto: ProtocolPair, config: BufferConfig, state: GameState
) -> List[EnvAction]:
    """Environment choices at a round start.

    Peer actions are always offered when enabled; the explicit handoff is
    offered only when no peer action exists (mandatory handoff case).
    """

    moves = enabled_moves(proto, config, state)
    if moves:
        return list(moves)
    return [PASS]


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def apply_env(state: GameState, action: EnvAction) -> GameState:
    """Apply an environment choice and return the resulting state."""

    if action is PASS:
        return state
    if not isinstance(action, PeerMove):
        raise ValueError(f"illegal environment action: {action!r}")

    if action.act is Act.SEND:
        direction = _send_direction(action.peer)
        pending = list(state.pending(direction))
        pending.append(action.message)
        if direction is Direction.A2B:
            return state._with_queues(
                pending,
                state.converted_a2b,
                state.pending_b2a,
                state.converted_b2a,
                state_a=action.target if action.peer == "a" else None,
                state_b=action.target if action.peer == "b" else None,
            )
        return state._with_queues(
            state.pending_a2b,
            state.converted_a2b,
            pending,
            state.converted_b2a,
            state_a=action.target if action.peer == "a" else None,
            state_b=action.target if action.peer == "b" else None,
        )

    direction = _recv_direction(action.peer)
    converted = list(state.converted(direction))
    if not converted or converted[0] != action.message:
        raise ValueError(
            f"illegal receive {action.message!r}: converted head is "
            f"{converted[0] if converted else None!r}"
        )
    converted.pop(0)
    if direction is Direction.A2B:
        return state._with_queues(
            state.pending_a2b,
            converted,
            state.pending_b2a,
            state.converted_b2a,
            state_a=action.target if action.peer == "a" else None,
            state_b=action.target if action.peer == "b" else None,
        )
    return state._with_queues(
        state.pending_a2b,
        state.converted_a2b,
        state.pending_b2a,
        converted,
        state_a=action.target if action.peer == "a" else None,
        state_b=action.target if action.peer == "b" else None,
    )


def apply_adapter(state: GameState, action: AdapterAction) -> GameState:
    """Apply an adapter conversion (or a wait) and return the new state."""

    if action is WAIT:
        return state
    if not isinstance(action, Convert):
        raise ValueError(f"illegal adapter action: {action!r}")

    direction = action.direction
    pending = list(state.pending(direction))
    if not pending or pending[0] != action.source_message:
        raise ValueError(
            f"illegal conversion: pending head {pending[0] if pending else None!r} "
            f"!= {action.source_message!r}"
        )
    pending.pop(0)
    converted = list(state.converted(direction))
    converted.append(action.destination_message)
    if direction is Direction.A2B:
        return state._with_queues(
            pending, converted, state.pending_b2a, state.converted_b2a
        )
    return state._with_queues(
        state.pending_a2b, state.converted_a2b, pending, converted
    )


@dataclass(frozen=True)
class MoveOutcome:
    """Result of playing one full round."""

    env_action: EnvAction
    adapter_action: AdapterAction
    mid_state: Optional[GameState]  # state after the environment move
    end_state: GameState
    terminal: bool
    deadlock: bool


def play_round(
    proto: ProtocolPair,
    config: BufferConfig,
    state: GameState,
    env_action: EnvAction,
    adapter_action: AdapterAction,
) -> MoveOutcome:
    """Play one environment + adapter round with explicit legality checks."""

    choices = admissible_env_choices(proto, config, state)
    if env_action not in choices:
        raise ValueError(f"environment action {env_action!r} not admissible here")

    if env_action is PASS:
        conversions = available_conversions(proto, config, state)
        if adapter_action is WAIT:
            # Forced handoff plus an adapter with nothing to do: the round
            # changes nothing on either side, which the problem declares a
            # failure.
            return MoveOutcome(env_action, adapter_action, None, state, False, True)
        if adapter_action not in conversions:
            raise ValueError(
                f"adapter action {adapter_action!r} not enabled here"
            )
        end = apply_adapter(state, adapter_action)
        return MoveOutcome(
            env_action, adapter_action, None, end, end.is_goal(proto), False
        )

    mid = apply_env(state, env_action)
    if mid.is_goal(proto):
        # The environment move itself reached the goal (buffers already
        # empty); the adapter's wait is vacuous and the game terminates.
        return MoveOutcome(env_action, WAIT, mid, mid, True, False)
    conversions = available_conversions(proto, config, mid)
    if adapter_action is not WAIT and adapter_action not in conversions:
        raise ValueError(
            f"adapter action {adapter_action!r} not enabled here"
        )
    end = apply_adapter(mid, adapter_action)
    return MoveOutcome(
        env_action, adapter_action, mid, end, end.is_goal(proto), False
    )


# ---------------------------------------------------------------------------
# Reachability and the winning-rank fixpoint
# ---------------------------------------------------------------------------


def enumerate_reachable(
    proto: ProtocolPair, config: BufferConfig
) -> List[GameState]:
    """Enumerate every state reachable from the initial state.

    Reachable states are bounded even without the round limit: peer states
    move along DAGs and every queue has a finite capacity.
    """

    start = GameState.initial(proto)
    seen = {start}
    order = [start]
    queue = deque([start])
    while queue:
        state = queue.popleft()
        for choice in admissible_env_choices(proto, config, state):
            if choice is PASS:
                mid = state
            else:
                mid = apply_env(state, choice)
                if mid not in seen:
                    seen.add(mid)
                    order.append(mid)
                    queue.append(mid)
                if mid.is_goal(proto):
                    continue
            if mid.is_goal(proto):
                continue
            for conversion in available_conversions(proto, config, mid):
                nxt = apply_adapter(mid, conversion)
                if nxt not in seen:
                    seen.add(nxt)
                    order.append(nxt)
                    queue.append(nxt)
            # WAIT keeps the state unchanged and therefore introduces no new
            # reachable state.
    return order


def compute_ranks(
    proto: ProtocolPair, config: BufferConfig
) -> dict:
    """Least number of rounds the adapter needs to force the goal.

    ``rank[s]`` is the minimax distance-to-goal: the adapter minimizes while
    the environment maximizes.  Goal states have rank 0; states from which
    the goal cannot be forced keep rank ``INF``.  The fixpoint is monotone
    and terminates because the peer graphs are acyclic and the queues are
    bounded.
    """

    states = enumerate_reachable(proto, config)
    rank = {s: (0 if s.is_goal(proto) else INF) for s in states}

    changed = True
    while changed:
        changed = False
        for state in states:
            if rank[state] == 0:
                continue
            choices = admissible_env_choices(proto, config, state)
            per_choice: List[int] = []
            impossible = False
            for choice in choices:
                if choice is PASS:
                    conversions = available_conversions(proto, config, state)
                    if not conversions:
                        impossible = True  # forced no-change round: failure
                        break
                    best = min(
                        rank[apply_adapter(state, conv)] for conv in conversions
                    )
                    if best >= INF:
                        impossible = True
                        break
                    per_choice.append(1 + best)
                    continue

                mid = apply_env(state, choice)
                if mid.is_goal(proto):
                    per_choice.append(1)
                    continue
                options = available_conversions(proto, config, mid) + [WAIT]
                best = min(rank[apply_adapter(mid, opt)] for opt in options)
                if best >= INF:
                    impossible = True
                    break
                per_choice.append(1 + best)

            if impossible:
                continue
            candidate = max(per_choice)  # environment picks the worst choice
            if candidate < rank[state]:
                rank[state] = candidate
                changed = True
    return rank
