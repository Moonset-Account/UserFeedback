"""Synthetic protocol pairs used by the tests and as documented examples.

Everything here is local, hand-built data; no network, files or accounts are
involved.  Each builder returns a ``(ProtocolPair, description)`` tuple.
"""

from __future__ import annotations

from .model import Act, MessageMapping, PeerFSM, PeerTransition, ProtocolPair


def simple_request_reply():
    """A sends REQ, B answers RESP.  The textbook two-way handshake."""

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a2",),
        transitions=(
            PeerTransition("a0", "a1", Act.SEND, "REQ"),
            PeerTransition("a1", "a2", Act.RECV, "RESP"),
        ),
        alphabet=("REQ", "RESP"),
    )
    peer_b = PeerFSM(
        name="B",
        initial="b0",
        finals=("b2",),
        transitions=(
            PeerTransition("b0", "b1", Act.RECV, "REQ"),
            PeerTransition("b1", "b2", Act.SEND, "RESP"),
        ),
        alphabet=("REQ", "RESP"),
    )
    mapping = MessageMapping(a2b={"REQ": "REQ"}, b2a={"RESP": "RESP"})
    return ProtocolPair(peer_a, peer_b, mapping), (
        "simple request/reply: A sends REQ, B answers RESP"
    )


def one_way_send():
    """Only one direction carries a message."""

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a1",),
        transitions=(PeerTransition("a0", "a1", Act.SEND, "PING"),),
        alphabet=("PING",),
    )
    peer_b = PeerFSM(
        name="B",
        initial="b0",
        finals=("b1",),
        transitions=(PeerTransition("b0", "b1", Act.RECV, "PING"),),
        alphabet=("PING",),
    )
    mapping = MessageMapping(a2b={"PING": "PING"}, b2a={})
    return ProtocolPair(peer_a, peer_b, mapping), "one-way A->B PING"


def already_terminated():
    """Both peers start and finish in the same state; goal at round zero."""

    peer_a = PeerFSM(
        name="A", initial="done", finals=("done",), transitions=(), alphabet=()
    )
    peer_b = PeerFSM(
        name="B", initial="done", finals=("done",), transitions=(), alphabet=()
    )
    mapping = MessageMapping(a2b={}, b2a={})
    return ProtocolPair(peer_a, peer_b, mapping), "trivial: both peers already final"


def nondeterministic_response():
    """B may answer with RESP_OK or RESP_ERR; the adapter must handle both."""

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a2",),
        transitions=(
            PeerTransition("a0", "a1", Act.SEND, "REQ"),
            PeerTransition("a1", "a2", Act.RECV, "ACK"),
        ),
        alphabet=("REQ", "ACK"),
    )
    peer_b = PeerFSM(
        name="B",
        initial="b0",
        finals=("b2",),
        transitions=(
            PeerTransition("b0", "b1", Act.RECV, "REQ"),
            PeerTransition("b1", "b2", Act.SEND, "RESP_OK"),
            PeerTransition("b1", "b2", Act.SEND, "RESP_ERR"),
        ),
        alphabet=("REQ", "RESP_OK", "RESP_ERR"),
    )
    mapping = MessageMapping(
        a2b={"REQ": "REQ"},
        b2a={"RESP_OK": "ACK", "RESP_ERR": "ACK"},
    )
    return ProtocolPair(peer_a, peer_b, mapping), (
        "nondeterministic B reply: both RESP_OK and RESP_ERR map to ACK"
    )


def capacity_two_contention():
    """Two pending slots per direction are genuinely required.

    Each peer emits three messages before its receive phase opens.  Under a
    single slot per FIFO the adapter can move at most one message of a
    direction out of the pending queue; the second send then fills the
    converted slot while the receiving peer is still sending, and the third
    send has nowhere to go -- the position deadlocks before any receive is
    enabled.  With two pending slots (the per-FIFO maximum) a whole burst of
    three can be staged: two in the pending FIFO and one converted ahead of
    the receive phase, after which the peers drain the aligned FIFOs.  So
    cap 1 fails with a deadlock leaf and cap 2 wins.
    """

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a6",),
        transitions=(
            PeerTransition("a0", "a1", Act.SEND, "REQ1"),
            PeerTransition("a1", "a2", Act.SEND, "REQ2"),
            PeerTransition("a2", "a3", Act.SEND, "REQ3"),
            PeerTransition("a3", "a4", Act.RECV, "ACK1"),
            PeerTransition("a4", "a5", Act.RECV, "ACK2"),
            PeerTransition("a5", "a6", Act.RECV, "ACK3"),
        ),
        alphabet=("REQ1", "REQ2", "REQ3", "ACK1", "ACK2", "ACK3"),
    )
    peer_b = PeerFSM(
        name="B",
        initial="b0",
        finals=("b6",),
        transitions=(
            PeerTransition("b0", "b1", Act.SEND, "ack1"),
            PeerTransition("b1", "b2", Act.SEND, "ack2"),
            PeerTransition("b2", "b3", Act.SEND, "ack3"),
            PeerTransition("b3", "b4", Act.RECV, "REQ1"),
            PeerTransition("b4", "b5", Act.RECV, "REQ2"),
            PeerTransition("b5", "b6", Act.RECV, "REQ3"),
        ),
        alphabet=("ack1", "ack2", "ack3", "REQ1", "REQ2", "REQ3"),
    )
    mapping = MessageMapping(
        a2b={"REQ1": "REQ1", "REQ2": "REQ2", "REQ3": "REQ3"},
        b2a={"ack1": "ACK1", "ack2": "ACK2", "ack3": "ACK3"},
    )
    return ProtocolPair(peer_a, peer_b, mapping), (
        "three sends before any receive: needs two pending slots per direction"
    )


def fifo_order_incompatible():
    """B sends X then Y, but A must consume Y before X: impossible under FIFO.

    No capacity up to the maximum of two per FIFO can fix a head-of-line
    ordering violation, so this instance is universally incompatible.
    """

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a2",),
        transitions=(
            PeerTransition("a0", "a1", Act.RECV, "y"),
            PeerTransition("a1", "a2", Act.RECV, "x"),
        ),
        alphabet=("x", "y"),
    )
    peer_b = PeerFSM(
        name="B",
        initial="b0",
        finals=("b2",),
        transitions=(
            PeerTransition("b0", "b1", Act.SEND, "X"),
            PeerTransition("b1", "b2", Act.SEND, "Y"),
        ),
        alphabet=("X", "Y"),
    )
    mapping = MessageMapping(
        a2b={}, b2a={"X": "x", "Y": "y"}
    )
    return ProtocolPair(peer_a, peer_b, mapping), (
        "FIFO inversion: B emits X then Y while A needs Y then X"
    )


def eager_conversion_blocks_reply():
    """An immediately enabled conversion can block the answer a peer needs.

    A sends ``OPEN`` and only then enters its receive phase, consuming
    ``INFO`` followed by ``GO``.  B, after receiving OPEN, deterministically
    emits ``info`` and then ``go``.  The trap is timing, not ordering: the
    mapping makes the info conversion enabled as soon as info lands, but A
    cannot receive until its OPEN has been delivered.  An *eager* adapter
    that converts info immediately fills the single B2A converted slot with
    a message nobody can drain; go then cannot be converted, the pending FIFO
    backs up and the run deadlocks.  The winning adapter *waits* until OPEN
    is consumed and A's receive phase opens, then converts info and go in
    order.  The test suite drives the same protocol with both an explicit
    greedy policy and the synthesized one, showing that a single happy path
    is no evidence of compatibility while a universally winning policy is.
    """

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a3",),
        transitions=(
            PeerTransition("a0", "a1", Act.SEND, "OPEN"),
            PeerTransition("a1", "a2", Act.RECV, "INFO"),
            PeerTransition("a2", "a3", Act.RECV, "GO"),
        ),
        alphabet=("OPEN", "GO", "INFO"),
    )
    peer_b = PeerFSM(
        name="B",
        initial="b0",
        finals=("b3",),
        transitions=(
            PeerTransition("b0", "b1", Act.RECV, "OPEN"),
            PeerTransition("b1", "b2", Act.SEND, "info"),
            PeerTransition("b2", "b3", Act.SEND, "go"),
        ),
        alphabet=("OPEN", "go", "info"),
    )
    mapping = MessageMapping(
        a2b={"OPEN": "OPEN"},
        b2a={"go": "GO", "info": "INFO"},
    )
    return ProtocolPair(peer_a, peer_b, mapping), (
        "eager info conversion before A can receive blocks go; the winning "
        "policy waits until the receive phase opens"
    )


def cyclic_peer():
    """A cyclic peer graph; construction must reject it."""

    peer_a = PeerFSM(
        name="A",
        initial="a0",
        finals=("a1",),
        transitions=(
            PeerTransition("a0", "a1", Act.SEND, "M"),
            PeerTransition("a1", "a0", Act.RECV, "N"),
        ),
        alphabet=("M", "N"),
    )
    peer_b = PeerFSM(
        name="B", initial="b0", finals=("b0",), transitions=(), alphabet=()
    )
    mapping = MessageMapping(a2b={}, b2a={})
    return ProtocolPair(peer_a, peer_b, mapping), "cyclic peer graph (invalid input)"


# Convenience fixture table used by the test suite.
FIXTURES = {
    "simple_request_reply": simple_request_reply,
    "one_way_send": one_way_send,
    "already_terminated": already_terminated,
    "nondeterministic_response": nondeterministic_response,
    "capacity_two_contention": capacity_two_contention,
    "fifo_order_incompatible": fifo_order_incompatible,
    "eager_conversion_blocks_reply": eager_conversion_blocks_reply,
}
