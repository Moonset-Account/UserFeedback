"""Tests for per-domain configuration, flag latching and mask semantics."""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from fpiso.domain import Domain
from fpiso.errors import FormatError
from fpiso.flags import Flag, MaskBits, flags_text, ordered_flags
from fpiso.format import SMALL, Format, RoundingMode


class MaskBitsTests(unittest.TestCase):
    def test_default_is_all_masked(self) -> None:
        masks = MaskBits.all_masked()
        self.assertEqual(masks.unmasked, ())
        for flag in Flag:
            self.assertTrue(masks.is_masked(flag))

    def test_unmasking_helper(self) -> None:
        masks = MaskBits.unmasking(Flag.UNDERFLOW, Flag.INEXACT)
        self.assertEqual(set(masks.unmasked), {Flag.UNDERFLOW, Flag.INEXACT})
        self.assertTrue(masks.is_masked(Flag.OVERFLOW))

    def test_all_unmasked(self) -> None:
        self.assertEqual(set(MaskBits.all_unmasked().unmasked), set(Flag))

    def test_rejects_non_bool(self) -> None:
        with self.assertRaises(ValueError):
            MaskBits(overflow=1)

    def test_describe_is_stable(self) -> None:
        self.assertEqual(
            MaskBits.all_masked().describe(),
            "overflow=masked underflow=masked inexact=masked",
        )


class FlagTextTests(unittest.TestCase):
    def test_canonical_order(self) -> None:
        self.assertEqual(
            ordered_flags([Flag.INEXACT, Flag.OVERFLOW, Flag.UNDERFLOW]),
            (Flag.OVERFLOW, Flag.UNDERFLOW, Flag.INEXACT),
        )

    def test_text(self) -> None:
        self.assertEqual(flags_text(set()), "{}")
        self.assertEqual(flags_text({Flag.INEXACT, Flag.OVERFLOW}), "{overflow, inexact}")


class DomainConfigurationTests(unittest.TestCase):
    def test_defaults(self) -> None:
        domain = Domain("d", SMALL)
        self.assertEqual(domain.rounding, RoundingMode.RNE)
        self.assertEqual(domain.masks, MaskBits.all_masked())
        self.assertEqual(domain.sticky, frozenset())
        self.assertEqual(domain.pending, ())

    def test_invalid_configuration(self) -> None:
        with self.assertRaises(FormatError):
            Domain("", SMALL)
        with self.assertRaises(FormatError):
            Domain("d", "small")  # type: ignore[arg-type]
        with self.assertRaises(FormatError):
            Domain("d", SMALL, "sideways")
        with self.assertRaises(FormatError):
            Domain("d", SMALL, masks={"overflow": False})  # type: ignore[arg-type]

    def test_with_rounding_copies_state(self) -> None:
        base = Domain("base", SMALL, RoundingMode.RNE)
        base.add(F(1, 3), 1)
        clone = base.with_rounding(RoundingMode.RU)
        self.assertEqual(clone.rounding, RoundingMode.RU)
        self.assertEqual(clone.sticky, base.sticky)
        # The two ledgers are independent: a tiny value in the RU clone adds
        # underflow, which the RNE base never sees.
        clone.add(F(1, 1000), 0)
        self.assertNotEqual(clone.sticky, base.sticky)
        self.assertIn(Flag.UNDERFLOW, clone.sticky)
        self.assertEqual(base.sticky, frozenset({Flag.INEXACT}))

    def test_copy_is_deep(self) -> None:
        base = Domain("base", SMALL, RoundingMode.RD)
        base.add(1, F(1, 16))
        clone = base.copy()
        clone.clear_flags()
        self.assertEqual(base.sticky, frozenset({Flag.INEXACT}))
        self.assertEqual(clone.sticky, frozenset())

    def test_format_and_mask_changes_return_new_domains(self) -> None:
        base = Domain("base", SMALL)
        wider = base.with_format(Format("wide", 6, -4, 4))
        self.assertEqual(wider.format.name, "wide")
        self.assertEqual(base.format.name, "small")
        relaxed = base.with_masks(MaskBits.unmasking(Flag.OVERFLOW))
        self.assertTrue(relaxed.masks.is_unmasked(Flag.OVERFLOW))
        self.assertTrue(base.masks.is_masked(Flag.OVERFLOW))


class LatchingTests(unittest.TestCase):
    def test_masked_flag_is_sticky_but_not_pending(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_masked())
        result = domain.add(F(1, 3), F(1, 3))
        self.assertEqual(result.flags, frozenset({Flag.INEXACT}))
        self.assertEqual(result.pending, ())
        self.assertEqual(domain.sticky, frozenset({Flag.INEXACT}))
        self.assertEqual(domain.pending, ())

    def test_unmasked_flag_is_sticky_and_pending(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        result = domain.add(F(1, 3), F(1, 3))
        self.assertEqual(result.pending, (Flag.INEXACT,))
        self.assertEqual(domain.sticky, frozenset({Flag.INEXACT}))
        self.assertEqual(domain.pending, (Flag.INEXACT,))

    def test_sticky_accumulates_across_operations(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_masked())
        domain.add(F(1, 3), F(1, 3))
        domain.mul(F(1, 64), F(1, 64))
        self.assertEqual(domain.sticky, frozenset({Flag.UNDERFLOW, Flag.INEXACT}))
        self.assertEqual(domain.raised, ())

    def test_raised_lists_only_unmasked_sticky(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.unmasking(Flag.UNDERFLOW))
        domain.add(F(1, 3), F(1, 3))
        domain.mul(F(1, 64), F(1, 64))
        self.assertEqual(domain.raised, (Flag.UNDERFLOW,))

    def test_take_pending_drains_only_the_queue(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        domain.add(F(1, 3), F(1, 3))
        drained = domain.take_pending()
        self.assertEqual(drained, (Flag.INEXACT,))
        self.assertEqual(domain.pending, ())
        self.assertEqual(domain.sticky, frozenset({Flag.INEXACT}))

    def test_clear_flags_clears_both(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        domain.add(F(1, 3), F(1, 3))
        domain.clear_flags()
        self.assertEqual(domain.sticky, frozenset())
        self.assertEqual(domain.pending, ())

    def test_mask_change_does_not_rewrite_history(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RNE, MaskBits.all_unmasked())
        domain.add(F(1, 3), F(1, 3))
        relaxed = domain.with_masks(MaskBits.all_masked())
        self.assertEqual(relaxed.pending, (Flag.INEXACT,))
        self.assertEqual(relaxed.sticky, frozenset({Flag.INEXACT}))
        # A new event in the masked copy does not queue.
        relaxed.add(F(1, 3), F(1, 3))
        self.assertEqual(relaxed.pending, (Flag.INEXACT,))


class StateSnapshotTests(unittest.TestCase):
    def test_state_is_immutable_snapshot(self) -> None:
        domain = Domain("d", SMALL, RoundingMode.RD, MaskBits.all_unmasked())
        before = domain.state()
        domain.add(F(1, 3), F(1, 3))
        after = domain.state()
        self.assertEqual(before.sticky, frozenset())
        self.assertEqual(after.sticky, frozenset({Flag.INEXACT}))
        self.assertNotEqual(before, after)

    def test_describe_mentions_every_component(self) -> None:
        domain = Domain("audit", SMALL, RoundingMode.RU, MaskBits.unmasking(Flag.UNDERFLOW))
        text = domain.state().describe()
        for fragment in ("'audit'", "small", "ru", "underflow=unmasked", "sticky={}"):
            self.assertIn(fragment, text)


class UnregisteredDomainTests(unittest.TestCase):
    def test_standalone_domain_is_a_pure_calculator(self) -> None:
        a = Domain("a", SMALL, RoundingMode.RNE)
        b = Domain("b", SMALL, RoundingMode.RU)
        a.add(F(1, 3), F(1, 3))
        b.add(F(1, 3), F(1, 3))
        self.assertEqual(a.sticky, frozenset({Flag.INEXACT}))
        self.assertEqual(b.sticky, frozenset({Flag.INEXACT}))


if __name__ == "__main__":
    unittest.main()
