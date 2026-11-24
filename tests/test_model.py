"""Tests for the data model: FSM validation, mapping and buffer bounds."""

import unittest

from adapter_synth.examples import cyclic_peer
from adapter_synth.model import (
    Act,
    BufferConfig,
    Direction,
    MessageMapping,
    PeerFSM,
    PeerTransition,
    ProtocolPair,
)


class TestPeerFSM(unittest.TestCase):
    def test_cyclic_peer_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            cyclic_peer()
        self.assertIn("cycle", str(ctx.exception))

    def test_self_loop_is_a_cycle(self):
        with self.assertRaises(ValueError):
            PeerFSM(
                name="X",
                initial="s0",
                finals=("s0",),
                transitions=(PeerTransition("s0", "s0", Act.SEND, "M"),),
                alphabet=("M",),
            )

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            PeerFSM(
                name="X",
                initial="s0",
                finals=("s1",),
                states=("s0", "s1"),
                transitions=(
                    PeerTransition("s0", "s9", Act.SEND, "M"),
                ),
                alphabet=("M",),
            )
        self.assertIn("undeclared state", str(ctx.exception))

    def test_undeclared_initial_rejected(self):
        with self.assertRaises(ValueError):
            PeerFSM(
                name="X",
                initial="s0",
                finals=("s1",),
                states=("s1",),
                transitions=(),
                alphabet=(),
            )

    def test_message_outside_alphabet_rejected(self):
        with self.assertRaises(ValueError):
            PeerFSM(
                name="X",
                initial="s0",
                finals=("s1",),
                transitions=(PeerTransition("s0", "s1", Act.SEND, "M"),),
                alphabet=("OTHER",),
            )

    def test_dag_accepted_and_topology_built(self):
        fsm = PeerFSM(
            name="X",
            initial="s0",
            finals=("s2",),
            transitions=(
                PeerTransition("s0", "s1", Act.SEND, "M"),
                PeerTransition("s1", "s2", Act.RECV, "N"),
            ),
            alphabet=("M", "N"),
        )
        self.assertLess(fsm.topo_index["s0"], fsm.topo_index["s2"])
        self.assertTrue(fsm.is_final("s2"))
        self.assertFalse(fsm.is_final("s1"))


class TestBufferConfig(unittest.TestCase):
    def test_capacity_bounds(self):
        for bad in (-1, 3):
            with self.assertRaises(ValueError):
                BufferConfig(bad, 0, 0, 0)

    def test_bool_rejected_as_capacity(self):
        with self.assertRaises(ValueError):
            BufferConfig(True, 0, 0, 0)  # type: ignore[arg-type]

    def test_total_and_tuple(self):
        cfg = BufferConfig(2, 1, 0, 2)
        self.assertEqual(cfg.total_capacity(), 5)
        self.assertEqual(cfg.as_tuple(), (2, 1, 0, 2))

    def test_zero_capacity_allowed(self):
        cfg = BufferConfig(0, 0, 0, 0)
        self.assertEqual(cfg.total_capacity(), 0)


class TestMessageMappingValidation(unittest.TestCase):
    def _peers(self):
        a = PeerFSM(
            name="A", initial="a0", finals=("a1",),
            transitions=(PeerTransition("a0", "a1", Act.SEND, "M"),),
            alphabet=("M",),
        )
        b = PeerFSM(
            name="B", initial="b0", finals=("b1",),
            transitions=(PeerTransition("b0", "b1", Act.RECV, "N"),),
            alphabet=("N",),
        )
        return a, b

    def test_valid_mapping_accepted(self):
        a, b = self._peers()
        pair = ProtocolPair(a, b, MessageMapping(a2b={"M": "N"}, b2a={}))
        self.assertEqual(pair.mapping.image(Direction.A2B, "M"), "N")

    def test_unknown_source_rejected(self):
        a, b = self._peers()
        with self.assertRaises(ValueError):
            ProtocolPair(a, b, MessageMapping(a2b={"NOPE": "N"}, b2a={}))

    def test_unknown_image_rejected(self):
        a, b = self._peers()
        with self.assertRaises(ValueError):
            ProtocolPair(a, b, MessageMapping(a2b={"M": "NOPE"}, b2a={}))


if __name__ == "__main__":
    unittest.main()
