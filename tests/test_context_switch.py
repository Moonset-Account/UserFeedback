"""Entry masking, scratch isolation, scheduling, exit and error cases."""

import unittest

from regswitch import (
    EntryPolicy,
    InvalidPolicyError,
    Machine,
    ModelError,
    StackError,
    TrustLevel,
    UnknownContextError,
    UnknownRegisterError,
)

SECRETS = {f"x{i}": 0xA5A5_0000 + i for i in range(4)}


class MachineFixture(unittest.TestCase):
    def make_machine(self):
        m = Machine(num_gprs=4, num_vector_pairs=2, pairs_per_group=1,
                    vector_lanes=2)
        m.add_domain("realm")
        m.add_domain("host")
        m.spawn_thread("app", domain="realm", trust=TrustLevel.PROTECTED)
        m.start("app")
        for name, value in SECRETS.items():
            m.write_gpr(name, value)
        return m


class EntryMaskingTest(MachineFixture):
    def test_low_trust_entry_zeroes_everything_not_kept(self):
        m = self.make_machine()
        m.add_handler("irq", TrustLevel.PUBLIC, "host",
                      EntryPolicy(keep_gprs={"x0"}))
        m.interrupt("irq")
        self.assertEqual(m.read_gpr("x0"), SECRETS["x0"])  # allowed to keep
        for name in ("x1", "x2", "x3"):
            self.assertEqual(m.read_gpr(name), 0, f"{name} must read as zero")
        m.audit()

    def test_low_trust_entry_with_empty_mask_zeroes_all(self):
        m = self.make_machine()
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.interrupt("irq")
        for name in SECRETS:
            self.assertEqual(m.read_gpr(name), 0)
        m.audit()

    def test_high_trust_entry_inherits_per_mask(self):
        m = self.make_machine()
        m.add_handler("fw", TrustLevel.PROTECTED, "realm",
                      EntryPolicy(keep_gprs={"x1", "x3"}))
        m.interrupt("fw")
        self.assertEqual(m.read_gpr("x0"), 0)   # not in mask
        self.assertEqual(m.read_gpr("x1"), SECRETS["x1"])
        self.assertEqual(m.read_gpr("x2"), 0)   # not in mask
        self.assertEqual(m.read_gpr("x3"), SECRETS["x3"])
        m.audit()

    def test_handler_scratch_does_not_leak_back(self):
        m = self.make_machine()
        m.add_handler("irq", TrustLevel.PUBLIC, "host",
                      EntryPolicy(keep_gprs={"x0"}))
        m.interrupt("irq")
        m.write_gpr("x0", 0xDEAD)
        m.write_gpr("x2", 0xBEEF)
        m.iret()
        for name, value in SECRETS.items():
            self.assertEqual(m.read_gpr(name), value,
                             f"{name} must be restored exactly")
        m.audit()

    def test_return_restores_exact_scalars_after_nested_entries(self):
        m = self.make_machine()
        m.add_handler("h1", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.add_handler("h2", TrustLevel.PROTECTED, "realm",
                      EntryPolicy(keep_gprs={"x0", "x1", "x2", "x3"}))
        m.interrupt("h1")
        m.write_gpr("x0", 1)
        m.interrupt("h2")  # inherits h1's scratch per mask
        self.assertEqual(m.read_gpr("x0"), 1)
        m.write_gpr("x0", 2)
        m.iret()
        self.assertEqual(m.read_gpr("x0"), 1)  # h1's own copy back
        m.iret()
        for name, value in SECRETS.items():
            self.assertEqual(m.read_gpr(name), value)
        m.audit()


class ScheduleTest(MachineFixture):
    def make_two_domain_machine(self):
        m = self.make_machine()
        m.spawn_thread("svc", domain="host", trust=TrustLevel.PUBLIC)
        return m

    def test_schedule_to_other_domain_and_back_restores_exactly(self):
        m = self.make_two_domain_machine()
        m.write_vector_pair(0, (0x11, 0x12), (0x13, 0x14))
        m.schedule("svc")
        # Fresh thread in another protection domain sees zeros only.
        for name in SECRETS:
            self.assertEqual(m.read_gpr(name), 0)
        self.assertEqual(m.read_vector_pair(0), ((0, 0), (0, 0)))
        m.write_gpr("x0", 0x77)
        m.write_vector_pair(1, (0x21, 0x22), (0x23, 0x24))
        m.schedule("app")
        for name, value in SECRETS.items():
            self.assertEqual(m.read_gpr(name), value)
        self.assertEqual(m.read_vector_pair(0), ((0x11, 0x12), (0x13, 0x14)))
        self.assertEqual(m.read_vector_pair(1), ((0, 0), (0, 0)))
        m.schedule("svc")
        self.assertEqual(m.read_gpr("x0"), 0x77)
        self.assertEqual(m.read_vector_pair(1), ((0x21, 0x22), (0x23, 0x24)))
        m.audit()

    def test_schedule_rejected_while_handler_active(self):
        m = self.make_two_domain_machine()
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.interrupt("irq")
        with self.assertRaises(StackError):
            m.schedule("svc")
        m.iret()
        m.schedule("svc")  # fine again at thread level
        m.audit()


class ThreadExitTest(MachineFixture):
    def test_exit_destroys_frame_and_owned_vector_groups(self):
        m = self.make_machine()
        m.spawn_thread("svc", domain="host", trust=TrustLevel.PUBLIC)
        m.write_vector_pair(0, (1, 2), (3, 4))  # app owns group 0
        m.exit_thread()
        self.assertIsNone(m.current_name)
        self.assertIsNone(m.group_owner_name(0))  # ownership dropped
        with self.assertRaises(UnknownContextError):
            m.frame_slots("app")  # frame is gone
        with self.assertRaises(StackError):
            m.read_gpr("x0")  # machine idle
        m.start("svc")
        self.assertEqual(m.read_vector_pair(0), ((0, 0), (0, 0)))
        m.audit()

    def test_exit_rejected_while_handler_active(self):
        m = self.make_machine()
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.interrupt("irq")
        with self.assertRaises(StackError):
            m.exit_thread()
        m.iret()
        m.audit()


class ErrorCaseTest(MachineFixture):
    def test_return_from_base_thread_rejected(self):
        m = self.make_machine()
        with self.assertRaises(StackError):
            m.iret()

    def test_unknown_thread_handler_domain_register(self):
        m = self.make_machine()
        with self.assertRaises(UnknownContextError):
            m.schedule("ghost")
        with self.assertRaises(UnknownContextError):
            m.interrupt("ghost")
        with self.assertRaises(UnknownContextError):
            m.spawn_thread("t2", domain="ghost", trust=TrustLevel.PUBLIC)
        with self.assertRaises(UnknownRegisterError):
            m.read_gpr("x99")
        with self.assertRaises(UnknownRegisterError):
            m.read_vector_reg(99)

    def test_invalid_policy_rejected(self):
        m = self.make_machine()
        with self.assertRaises(InvalidPolicyError):
            m.add_handler("bad1", TrustLevel.PUBLIC, "host",
                          EntryPolicy(keep_gprs={"x42"}))
        with self.assertRaises(InvalidPolicyError):
            m.add_handler("bad2", TrustLevel.PUBLIC, "host",
                          EntryPolicy(keep_vector_pairs={7}))

    def test_duplicate_names_rejected(self):
        m = self.make_machine()
        with self.assertRaises(ModelError):
            m.spawn_thread("app", domain="realm", trust=TrustLevel.PUBLIC)
        with self.assertRaises(ModelError):
            m.add_domain("realm")
        m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())
        with self.assertRaises(ModelError):
            m.add_handler("irq", TrustLevel.PUBLIC, "host", EntryPolicy())

    def test_bad_machine_geometry_rejected(self):
        with self.assertRaises(ValueError):
            Machine(num_vector_pairs=3, pairs_per_group=2)
        with self.assertRaises(ValueError):
            Machine(num_gprs=0)

    def test_bad_vector_width_rejected(self):
        m = self.make_machine()
        with self.assertRaises(ValueError):
            m.write_vector_reg(0, (1, 2, 3))  # machine has 2 lanes


if __name__ == "__main__":
    unittest.main()
