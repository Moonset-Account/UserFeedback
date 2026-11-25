from __future__ import annotations

import unittest

from forensic_reader import (
    ChipValidationError,
    ForensicReader,
    Wiring,
    WiringConfigurationError,
    generate_wirings,
)


class ReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chips = {
            "A": bytes([0x00, 0x01, 0x02, 0x03]),
            "B": bytes([0x10, 0x11, 0x12, 0x13]),
        }
        self.wirings = [
            {"id": "AB", "order": ["A", "B"]},
            {"id": "BA", "order": ["B", "A"]},
        ]

    def test_word_sized_turn_taking_interleave(self) -> None:
        reader = ForensicReader(self.chips, self.wirings, word_size=2)
        self.assertEqual(reader.image_for("AB").hex(), "0001101102031213")
        self.assertEqual(reader.image_for("BA").hex(), "1011000112130203")
        self.assertEqual(reader.image_length, 8)

    def test_word_reversal_is_inside_each_input_word(self) -> None:
        wirings = [
            Wiring(id="word-swap-A", order=("A", "B"), reversal={"A": "word"}),
            Wiring(id="chip-reverse-A", order=("A", "B"), reversal={"A": "chip"}),
        ]
        reader = ForensicReader(self.chips, wirings, word_size=2)
        self.assertEqual(reader.image_for("word-swap-A").hex(), "0100101103021213")
        self.assertEqual(reader.image_for("chip-reverse-A").hex(), "0302101101001213")

    def test_boolean_reversal_alias(self) -> None:
        reader = ForensicReader(
            self.chips,
            [{"id": "rev", "order": ["A", "B"], "reverse": True}],
            word_size=2,
        )
        # A scalar boolean applies the whole-chip reversal to every chip.
        self.assertEqual(reader.image_for("rev").hex(), "0302131201001110")

        per_chip = ForensicReader(
            self.chips,
            [{"id": "rev-a", "order": ["A", "B"], "reversal": {"A": True}}],
            word_size=2,
        )
        self.assertEqual(per_chip.image_for("rev-a").hex(), "0302101101001213")

    def test_generated_cartesian_product(self) -> None:
        wirings = generate_wirings(
            ("A", "B"),
            permutations=[("A", "B"), ("B", "A")],
            reversal_modes={"A": ["none", "word"], "B": "none"},
        )
        self.assertEqual(len(wirings), 4)
        reader = ForensicReader(self.chips, wirings, word_size=2)
        self.assertEqual(reader.candidate_count, 4)
        word_candidates = [w for w in reader.wirings.values() if w.mode_for("A") == "word"]
        self.assertEqual(len(word_candidates), 2)

    def test_all_permutations_are_generated_by_default(self) -> None:
        chips = {0: b"0123", 1: b"4567", 2: b"89ab"}
        reader = ForensicReader(chips, word_size=1)
        self.assertEqual(reader.candidate_count, 6)
        orders = {w.order for w in reader.wirings.values()}
        self.assertEqual(len(orders), 6)

    def test_unequal_chip_lengths_rejected(self) -> None:
        with self.assertRaises(ChipValidationError):
            ForensicReader({"A": b"00", "B": b"0000"}, word_size=1)

    def test_order_must_be_permutation(self) -> None:
        with self.assertRaises(WiringConfigurationError):
            ForensicReader(
                self.chips,
                [{"id": "bad", "order": ["A", "A"]}],
                word_size=1,
            )

    def test_unknown_reversal_mode_rejected(self) -> None:
        with self.assertRaises(WiringConfigurationError):
            ForensicReader(
                self.chips,
                [{"id": "bad", "order": ["A", "B"], "reversal": {"A": "sideways"}}],
                word_size=1,
            )

    def test_sequence_chips_get_integer_channel_ids(self) -> None:
        reader = ForensicReader([b"\x00\x01", b"\x02\x03"], word_size=1)
        self.assertEqual(reader.chip_ids, (0, 1))
        self.assertEqual(reader.image_for(reader.wiring_ids[0]), b"\x00\x02\x01\x03")


if __name__ == "__main__":
    unittest.main()
