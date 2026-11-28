"""Lazy vector-group ownership: deferred save, first-use transfer, masks."""

import unittest

from regswitch import EntryPolicy, Machine, TrustLevel

APP_PAIR0 = ((0xB0, 0xB1), (0xB2, 0xB3))
APP_PAIR1 = ((0xC0, 0xC1), (0xC2, 0xC3))


class LazyMachineFixture(unittest.TestCase):
    """Machine with 2 pairs per lazy group; app owns group 0 (pairs 0-1)."""

    def make_machine(self, pairs_per_group=2):
        m = Machine(num_gprs=4, num_vector_pairs=2, pairs_per_group=1,
                    vector_lanes=2) if pairs_per_group == 1 else Machine(
            num_gprs=4, num_vector_pairs=4, pairs_per_group=2, vector_lanes=2)
        m.add_domain("realm")
        m.add_domain("host")
        m.spawn_thread("app", domain="realm", trust=TrustLevel.PROTECTED)
        m.start("app")
        if pairs_per_group == 1:
            m.write_vector_pair(0, *APP_PAIR0)
            m.write_vector_pair(1, *APP_PAIR1)
        else:
            m.write_vector_pair(0, *APP_PAIR0)
            m.write_vector_pair(1, *APP_PAIR1)
            m.write_vector_pair(2, (0xD0, 0xD1), (0xD2, 0xD3))
            m.write_vector_pair(3, (0xE0, 0xE1), (0xE2, 0xE3))
        return m


class DeferredSaveTest(LazyMachineFixture):
    def test_no_vector_save_until_first_use(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.interrupt("irq")
        # Interrupt arrived before any lazy save: app's frame holds GPRs
        # only, the vector groups are still live and owned by app.
        self.assertFalse(any(s.startswith("vgrp")
                             for s in m.frame_slots("app")))
        self.assertEqual(m.group_owner_name(0), "app")
        self.assertEqual(m.group_owner_name(1), "app")
        m.write_gpr("x0", 0x1234)  # handler uses GPRs only
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        self.assertEqual(m.read_vector_pair(1), APP_PAIR1)
        m.audit()

    def test_first_use_transfers_ownership_and_zeroes_for_low_trust(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        h = m.interrupt("irq")
        self.assertEqual(m.read_vector_pair(0), ((0, 0), (0, 0)))
        self.assertEqual(m.group_owner_name(0), h)
        # The interrupted owner's authoritative copy was sealed, untouched.
        self.assertEqual(m.frame_snapshot("app")["vgrp0"], APP_PAIR0)
        # Group 1 was never used: still live, still owned by app.
        self.assertEqual(m.group_owner_name(1), "app")
        self.assertNotIn("vgrp1", m.frame_slots("app"))
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        m.audit()

    def test_handler_vector_scratch_does_not_leak_back(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.interrupt("irq")
        m.write_vector_pair(0, (0xFF, 0xFF), (0xFF, 0xFF))
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        m.audit()


class InheritanceTest(LazyMachineFixture):
    def test_high_trust_handler_inherits_kept_group(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("fw", TrustLevel.PROTECTED, "realm",
                      EntryPolicy(keep_vector_pairs={0}))
        m.interrupt("fw")
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)  # inherited
        self.assertEqual(m.read_vector_pair(1), ((0, 0), (0, 0)))  # not kept
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        m.audit()

    def test_equal_trust_without_mask_still_zeroes(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("fw", TrustLevel.PROTECTED, "realm", EntryPolicy())
        m.interrupt("fw")
        self.assertEqual(m.read_vector_pair(0), ((0, 0), (0, 0)))
        m.iret()
        m.audit()

    def test_low_trust_handler_never_inherits_even_with_mask(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("irq", TrustLevel.PUBLIC, "host",
                      EntryPolicy(keep_vector_pairs={0}))
        m.interrupt("irq")
        # Mask lists pair 0, but PUBLIC < PROTECTED: no inheritance.
        self.assertEqual(m.read_vector_pair(0), ((0, 0), (0, 0)))
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        m.audit()


class GroupGranularityTest(LazyMachineFixture):
    def test_group_of_pairs_acquired_as_a_unit(self):
        m = self.make_machine(pairs_per_group=2)  # group 0 = pairs {0, 1}
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        h = m.interrupt("irq")
        # Touching one register of pair 0 acquires the whole group.
        self.assertEqual(m.read_vector_reg(0), (0, 0))
        self.assertEqual(m.group_owner_name(0), h)
        # The untouched pair 1 of the same group is zeroed too.
        self.assertEqual(m.read_vector_pair(1), ((0, 0), (0, 0)))
        # Sealed copy holds both original pairs.
        self.assertEqual(m.frame_snapshot("app")["vgrp0"],
                         APP_PAIR0 + APP_PAIR1)
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        self.assertEqual(m.read_vector_pair(1), APP_PAIR1)
        m.audit()

    def test_interrupt_before_lazy_save_two_levels_down(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("h1", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.add_handler("h2", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.interrupt("h1")
        m.interrupt("h2")  # two preemptions, still no vector save at all
        self.assertFalse(any(s.startswith("vgrp")
                             for s in m.frame_slots("app")))
        # h2's first use reaches straight through to app's live copy.
        self.assertEqual(m.read_vector_pair(1), ((0, 0), (0, 0)))
        self.assertEqual(m.frame_snapshot("app")["vgrp1"], APP_PAIR1)
        m.iret()
        m.iret()
        self.assertEqual(m.read_vector_pair(1), APP_PAIR1)
        m.audit()

    def test_preempted_handler_resumes_its_own_vector_copy(self):
        m = self.make_machine(pairs_per_group=1)
        m.add_handler("h1", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.add_handler("h2", TrustLevel.PUBLIC, "host", EntryPolicy())
        h1 = m.interrupt("h1")
        m.read_vector_pair(0)  # h1 takes ownership (zeros)
        m.write_vector_pair(0, (0x71, 0x72), (0x73, 0x74))
        m.interrupt("h2")
        # h2's first use seals h1's live copy into h1's frame.
        self.assertEqual(m.read_vector_pair(0), ((0, 0), (0, 0)))
        self.assertEqual(m.frame_snapshot(h1)["vgrp0"],
                         ((0x71, 0x72), (0x73, 0x74)))
        m.write_vector_pair(0, (0x99, 0x99), (0x99, 0x99))
        m.iret()
        # h1 resumes its own copy; h2's scribble is gone.
        self.assertEqual(m.read_vector_pair(0), ((0x71, 0x72), (0x73, 0x74)))
        m.iret()
        self.assertEqual(m.read_vector_pair(0), APP_PAIR0)
        m.audit()


if __name__ == "__main__":
    unittest.main()
