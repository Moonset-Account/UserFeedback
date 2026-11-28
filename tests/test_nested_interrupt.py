"""Three-level nested interrupt trace.

Scenario
--------
* ``app``   — protected thread in domain ``realm`` holding secret scalars
  (x0..x7) and secret vectors (pairs 0..3, i.e. lazy groups 0 and 1).
* ``uart``  — PUBLIC handler (level 1), may keep x0 only.
* ``wdt``   — PROTECTED handler (level 2, nested in uart), may keep
  x0..x3 and vector pairs {0, 1} (group 0).
* ``net``   — PUBLIC handler (level 3, nested in wdt), keeps nothing.

The trace drives interrupts, lazy first-use of vector groups at every
level, and the unwinding returns.  It asserts that every low-trust read
of a secret register is zero and that, after full unwinding, ``app``
gets back its exact scalar and vector contents.
"""

import unittest

from regswitch import EntryPolicy, Machine, TrustLevel

APP_GPRS = {f"x{i}": 0xA000 + i for i in range(8)}


def vec(k):
    """Deterministic, distinct 4-lane value for vector register v{k}."""
    return tuple(0xB000 + 0x10 * k + lane for lane in range(4))


APP_VECS = {k: vec(k) for k in range(8)}  # v0..v7

H1_PAIR0 = ((0x1110, 0x1111, 0x1112, 0x1113), (0x1114, 0x1115, 0x1116, 0x1117))
H1_PAIR1 = ((0x1120, 0x1121, 0x1122, 0x1123), (0x1124, 0x1125, 0x1126, 0x1127))
H2_PAIR0 = ((0x2220, 0x2221, 0x2222, 0x2223), (0x2224, 0x2225, 0x2226, 0x2227))
H3_PAIR2 = ((0x3330, 0x3331, 0x3332, 0x3333), (0x3334, 0x3335, 0x3336, 0x3337))

ZERO4 = (0, 0, 0, 0)


class ThreeLevelNestedInterruptTraceTest(unittest.TestCase):
    def setUp(self):
        m = Machine(num_gprs=8, num_vector_pairs=4, pairs_per_group=2,
                    vector_lanes=4)
        m.add_domain("realm")
        m.add_domain("host")
        m.spawn_thread("app", domain="realm", trust=TrustLevel.PROTECTED)
        m.add_handler("uart", TrustLevel.PUBLIC, "host",
                      EntryPolicy(keep_gprs={"x0"}))
        m.add_handler("wdt", TrustLevel.PROTECTED, "realm",
                      EntryPolicy(keep_gprs={"x0", "x1", "x2", "x3"},
                                  keep_vector_pairs={0, 1}))
        m.add_handler("net", TrustLevel.PUBLIC, "host", EntryPolicy())
        m.start("app")
        for name, value in APP_GPRS.items():
            m.write_gpr(name, value)
        for k in range(8):
            m.write_vector_reg(k, APP_VECS[k])
        self.m = m

    def test_three_level_nested_trace(self):
        m = self.m

        # ---- level 1: PUBLIC uart handler preempts the protected app ----
        h1 = m.interrupt("uart")
        self.assertEqual(m.read_gpr("x0"), APP_GPRS["x0"])  # kept by policy
        for name in ("x1", "x2", "x3", "x4", "x5", "x6", "x7"):
            self.assertEqual(m.read_gpr(name), 0,
                             f"low-trust read of {name} must be zero")
        # Interrupt happened before any lazy vector save: groups 0 and 1
        # are still live in the register file, owned by the suspended app.
        self.assertEqual(m.group_owner_name(0), "app")
        self.assertEqual(m.group_owner_name(1), "app")
        self.assertFalse(any(s.startswith("vgrp")
                             for s in m.frame_slots("app")))

        m.write_gpr("x0", 0xBEEF)
        m.write_gpr("x1", 0x1111)  # uart's private scratch
        # uart's first use of group 0: ownership transfer, secrets sealed.
        self.assertEqual(m.read_vector_reg(0), ZERO4)
        self.assertEqual(m.group_owner_name(0), h1)
        self.assertEqual(m.frame_snapshot("app")["vgrp0"],
                         (vec(0), vec(1), vec(2), vec(3)))
        m.write_vector_pair(0, *H1_PAIR0)
        m.write_vector_pair(1, *H1_PAIR1)
        m.audit()

        # ---- level 2: PROTECTED wdt handler preempts uart ---------------
        h2 = m.interrupt("wdt")
        # Inheritance per mask from uart's live state.
        self.assertEqual(m.read_gpr("x0"), 0xBEEF)
        self.assertEqual(m.read_gpr("x1"), 0x1111)
        self.assertEqual(m.read_gpr("x2"), 0)
        self.assertEqual(m.read_gpr("x3"), 0)
        for name in ("x4", "x5", "x6", "x7"):
            self.assertEqual(m.read_gpr(name), 0)
        # wdt keeps pairs {0,1} and PROTECTED >= PUBLIC: inherits uart's
        # live group-0 contents on first use.
        self.assertEqual(m.read_vector_pair(0), H1_PAIR0)
        self.assertEqual(m.read_vector_pair(1), H1_PAIR1)
        self.assertEqual(m.group_owner_name(0), h2)
        m.write_vector_pair(0, *H2_PAIR0)
        m.write_gpr("x0", 0xC0DE)
        m.write_gpr("x4", 0x2222)
        m.audit()

        # ---- level 3: PUBLIC net handler preempts wdt -------------------
        h3 = m.interrupt("net")
        for name in APP_GPRS:
            self.assertEqual(m.read_gpr(name), 0,
                             f"low-trust read of {name} must be zero")
        # net's first use of group 1: two nested interrupts happened
        # before this lazy save; the copy sealed now is still app's
        # original, taken straight from the live register file.
        self.assertEqual(m.read_vector_reg(4), ZERO4)
        self.assertEqual(m.group_owner_name(1), h3)
        self.assertEqual(m.frame_snapshot("app")["vgrp1"],
                         (vec(4), vec(5), vec(6), vec(7)))
        m.write_vector_pair(2, *H3_PAIR2)
        m.write_gpr("x7", 0x9999)  # net's scratch, must not leak
        m.audit()

        # ---- unwind level 3: net returns to wdt -------------------------
        self.assertEqual(m.iret(), h2)
        self.assertEqual(m.read_gpr("x0"), 0xC0DE)
        self.assertEqual(m.read_gpr("x1"), 0x1111)
        self.assertEqual(m.read_gpr("x4"), 0x2222)
        for name in ("x2", "x3", "x5", "x6", "x7"):
            self.assertEqual(m.read_gpr(name), 0)
        # wdt still owns group 0; its own contents are intact.
        self.assertEqual(m.read_vector_pair(0), H2_PAIR0)
        # Group 1's live owner (net) is gone and app's copy is sealed in
        # app's private frame, so wdt's first use of it reads zeros.
        self.assertEqual(m.read_vector_pair(2), (ZERO4, ZERO4))
        self.assertEqual(m.group_owner_name(1), h2)
        m.audit()

        # ---- unwind level 2: wdt returns to uart ------------------------
        self.assertEqual(m.iret(), h1)
        self.assertEqual(m.read_gpr("x0"), 0xBEEF)
        self.assertEqual(m.read_gpr("x1"), 0x1111)
        for name in ("x2", "x3", "x4", "x5", "x6", "x7"):
            self.assertEqual(m.read_gpr(name), 0)
        # uart gets its own group-0 contents back, not wdt's overwrite.
        self.assertEqual(m.read_vector_pair(0), H1_PAIR0)
        self.assertEqual(m.read_vector_pair(1), H1_PAIR1)
        m.audit()

        # ---- unwind level 1: uart returns to app ------------------------
        self.assertEqual(m.iret(), "app")
        # The original thread gets back its exact scalar contents.
        for name, value in APP_GPRS.items():
            self.assertEqual(m.read_gpr(name), value,
                             f"{name} must be restored exactly")
        # ... and its exact vector contents, for every register of both
        # lazily-saved groups.
        for k in range(8):
            self.assertEqual(m.read_vector_reg(k), APP_VECS[k],
                             f"v{k} must be restored exactly")
        # Nothing remains sealed anywhere; the invariant holds.
        self.assertEqual(m.frame_slots("app"), frozenset())
        m.audit()


if __name__ == "__main__":
    unittest.main()
