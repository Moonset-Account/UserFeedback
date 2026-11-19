"""Sequence-indexed naming history.

The index keeps one immutable :class:`~naming_history.model.Namespace`
snapshot per committed event.  Sequence ``0`` is the initial tree; sequence
``k`` is the state just after the ``k``-th event was committed.

An event that fails validation raises before a snapshot is appended, so a
rejected operation never advances the sequence number and can never be
observed by a query.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .model import (
    FutureObjectError,
    GoneObjectError,
    InvalidEventError,
    InvalidQueryError,
    Namespace,
    UnknownObjectError,
)


def _field(event: Dict[str, Any], key: str, op: str) -> Any:
    if key not in event:
        raise InvalidEventError(
            "event %r is missing field %r" % (op, key)
        )
    return event[key]


def _position(value: Any, label: str) -> tuple:
    if not isinstance(value, dict):
        raise InvalidEventError(
            "%s must be an object with 'dir' and 'name'" % (label,)
        )
    for key in ("dir", "name"):
        if key not in value:
            raise InvalidEventError(
                "%s is missing field %r" % (label, key)
            )
    return value["dir"], value["name"]


def apply_event(namespace: Namespace, event: Dict[str, Any]) -> Namespace:
    """Validate ``event`` against ``namespace`` and return the next state."""

    if not isinstance(event, dict):
        raise InvalidEventError("an event must be a JSON object")
    op = event.get("op")
    if op == "link":
        return namespace.link(
            _field(event, "dir", op),
            _field(event, "name", op),
            _field(event, "id", op),
        )
    if op == "unlink":
        return namespace.unlink(
            _field(event, "dir", op),
            _field(event, "name", op),
        )
    if op == "rename":
        src_dir, src_name = _position(
            _field(event, "from", op), "rename.from"
        )
        dst_dir, dst_name = _position(
            _field(event, "to", op), "rename.to"
        )
        return namespace.rename(src_dir, src_name, dst_dir, dst_name)
    if op == "exchange":
        a_dir, a_name = _position(_field(event, "a", op), "exchange.a")
        b_dir, b_name = _position(_field(event, "b", op), "exchange.b")
        return namespace.exchange(a_dir, a_name, b_dir, b_name)
    raise InvalidEventError("unknown event op %r" % (op,))


class NamingHistoryIndex:
    """Immutable snapshots addressed by committed event sequence number."""

    def __init__(self, initial: Namespace) -> None:
        if not isinstance(initial, Namespace):
            raise TypeError("initial state must be a Namespace")
        self._states: List[Namespace] = [initial]

    @property
    def committed_events(self) -> int:
        return len(self._states) - 1

    @property
    def last_seq(self) -> int:
        return len(self._states) - 1

    def _check_seq(self, seq: int) -> None:
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise InvalidQueryError(
                "sequence number must be an integer, got %r" % (seq,)
            )
        if seq < 0 or seq > self.last_seq:
            raise InvalidQueryError(
                "sequence %d is outside the committed history 0..%d"
                % (seq, self.last_seq)
            )

    def namespace_at(self, seq: int) -> Namespace:
        self._check_seq(seq)
        return self._states[seq]

    def paths_at(self, seq: int, obj_id: str) -> List[str]:
        """All paths of ``obj_id`` at the moment event ``seq`` is committed.

        "Not there yet" and "already gone" are reported distinctly so an
        investigator can tell a future object apart from a removed one.
        """

        self._check_seq(seq)
        snapshot = self._states[seq]
        if not isinstance(obj_id, str) or obj_id not in snapshot.kind:
            if isinstance(obj_id, str):
                if any(obj_id in state.kind for state in self._states[: seq + 1]):
                    raise GoneObjectError(
                        "object %r had paths earlier but has left the "
                        "namespace by sequence %d" % (obj_id, seq)
                    )
                if any(obj_id in state.kind for state in self._states[seq + 1 :]):
                    raise FutureObjectError(
                        "object %r is only introduced after sequence %d"
                        % (obj_id, seq)
                    )
            raise UnknownObjectError(
                "object %r is not known at this point in history" % (obj_id,)
            )
        return snapshot.paths_of(obj_id)

    def all_paths_at(self, seq: int) -> Dict[str, List[str]]:
        self._check_seq(seq)
        return self._states[seq].all_paths()

    def commit(self, event: Dict[str, Any]) -> int:
        """Validate and commit one event; return its sequence number."""

        next_state = apply_event(self._states[-1], event)
        self._states.append(next_state)
        return len(self._states) - 1

    def apply_events(self, events: Iterable[Dict[str, Any]]) -> "NamingHistoryIndex":
        for event in events:
            self.commit(event)
        return self
