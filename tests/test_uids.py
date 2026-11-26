import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deid_gateway import (  # noqa: E402
    MappingConflictError,
    ScriptedShortCodeGenerator,
    ShortCodeGenerator,
    UidMapper,
    is_legal_uid,
)
from deid_gateway.uids import canonical_short_code  # noqa: E402


class LegalUidTests(unittest.TestCase):
    def test_generated_uids_are_legal_numeric_dot_strings(self):
        mapper = UidMapper(base_root="1.2.840.999999.1")
        for i in range(200):
            uid = mapper.map_uid(f"source-{i}", "proj")
            self.assertTrue(is_legal_uid(uid), uid)
            self.assertLessEqual(len(uid), 64)
            for arc in uid.split("."):
                self.assertTrue(arc.isdigit())
                if len(arc) > 1:
                    self.assertNotEqual(arc[0], "0", f"zero-prefixed arc in {uid}")

    def test_validator_boundaries(self):
        valid = [
            "0",
            "1.2",
            "1.2.3.4",
            "2.25.12345678901234567890",
            "1." + ".".join(["1"] * 30),
        ]
        for value in valid:
            self.assertTrue(is_legal_uid(value), value)

        too_long = "1.2." + "1" * 70
        self.assertGreater(len(too_long), 64)
        invalid = [
            "",
            None,
            123,
            "1.2.3a",
            "1.2..3",
            ".1.2",
            "1.2.3.",
            "1.02.3",   # leading zero
            "1.2.-3",
            "uid:1.2.3",
            too_long,
        ]
        for value in invalid:
            self.assertFalse(is_legal_uid(value), repr(value))

    def test_canonical_short_code_normalizes_zero_padding(self):
        self.assertEqual(canonical_short_code(7), "7")
        self.assertEqual(canonical_short_code("007"), "7")
        self.assertEqual(canonical_short_code("0"), "0")
        with self.assertRaises(Exception):
            canonical_short_code("A7")


class GeneratorTests(unittest.TestCase):
    def test_monotonic_per_namespace(self):
        gen = ShortCodeGenerator()
        self.assertEqual([gen.next_code("p") for _ in range(3)], ["1", "2", "3"])
        self.assertEqual(gen.next_code("q"), "1")

    def test_seed_advances_high_water(self):
        gen = ShortCodeGenerator()
        gen.seed("p", 41)
        self.assertEqual(gen.next_code("p"), "42")

    def test_scripted_collision_generator(self):
        gen = ScriptedShortCodeGenerator(["5", "5"])
        self.assertEqual(gen.next_code("p"), "5")
        self.assertEqual(gen.next_code("p"), "5")
        # script exhausted -> monotonic counter from the default start
        self.assertEqual(gen.next_code("p"), "1")
        self.assertEqual(gen.next_code("p"), "2")


class ProjectIsolationTests(unittest.TestCase):
    def test_same_source_different_projects_get_different_uids(self):
        mapper = UidMapper(base_root="1.2.3.4")
        a = mapper.map_uid("same-original", "hospital-a")
        b = mapper.map_uid("same-original", "hospital-b")
        self.assertNotEqual(a, b)
        self.assertTrue(is_legal_uid(a) and is_legal_uid(b))
        # stability inside each receiving project
        self.assertEqual(mapper.map_uid("same-original", "hospital-a"), a)
        self.assertEqual(mapper.map_uid("same-original", "hospital-b"), b)
        # reverse resolution is project scoped
        self.assertEqual(mapper.resolve(a, "hospital-a"), "same-original")
        self.assertIsNone(mapper.resolve(a, "hospital-b"))

    def test_default_project_namespace(self):
        mapper = UidMapper(base_root="1.2.3.4")
        uid = mapper.map_uid("u")
        self.assertEqual(mapper.map_uid("u", None), uid)
        self.assertEqual(mapper.map_uid("u", ""), uid)
        self.assertEqual(mapper.map_uid("u", "default"), uid)


class CollisionSafetyTests(unittest.TestCase):
    def test_injected_duplicate_short_code_does_not_reassign_issued_number(self):
        gen = ScriptedShortCodeGenerator(["7", "7", "8"])
        mapper = UidMapper(base_root="1.2.3.4", code_generator=gen)

        issued_a = mapper.map_uid("instance-A", "p")
        self.assertEqual(issued_a, "1.2.3.4.1.7")

        issued_b = mapper.map_uid("instance-B", "p")
        self.assertNotEqual(issued_b, issued_a)
        self.assertEqual(issued_b, "1.2.3.4.1.8")

        # The already external number still belongs to A and A is stable.
        self.assertEqual(mapper.map_uid("instance-A", "p"), issued_a)
        self.assertEqual(mapper.resolve(issued_a, "p"), "instance-A")
        self.assertEqual(mapper.resolve(issued_b, "p"), "instance-B")
        self.assertTrue(
            any(e["kind"] == "short_code_collision" for e in mapper.collision_events)
        )

    def test_adversarial_permanent_collision_still_unique_and_stable(self):
        mapper = UidMapper(
            base_root="1.2.3.4",
            code_generator=ScriptedShortCodeGenerator.always(7),
        )
        uids = {name: mapper.map_uid(name, "p") for name in ("A", "B", "C", "D")}
        self.assertEqual(len(set(uids.values())), 4)
        owner = mapper.resolve("1.2.3.4.1.7", "p")
        self.assertEqual(owner, "A")
        for name, pseudo in uids.items():
            self.assertEqual(mapper.map_uid(name, "p"), pseudo)
            self.assertEqual(mapper.resolve(pseudo, "p"), name)
            self.assertTrue(is_legal_uid(pseudo))

    def test_bind_rejects_reassignment_in_both_directions(self):
        mapper = UidMapper(base_root="1.2.3.4")
        mapper.bind("u1", "1.2.3.4.9.1", "p")
        # idempotent same pair
        mapper.bind("u1", "1.2.3.4.9.1", "p")
        # pseudonym already issued to another source
        with self.assertRaises(MappingConflictError):
            mapper.bind("u2", "1.2.3.4.9.1", "p")
        # source already bound to another pseudonym
        with self.assertRaises(MappingConflictError):
            mapper.bind("u1", "1.2.3.4.9.2", "p")
        # nothing changed after rejected writes
        self.assertEqual(mapper.resolve("1.2.3.4.9.1", "p"), "u1")
        self.assertIsNone(mapper.resolve("1.2.3.4.9.2", "p"))

    def test_collision_safety_with_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            gen = ScriptedShortCodeGenerator.always(5)
            mapper = UidMapper(path, base_root="1.2.3.4", code_generator=gen)
            first = mapper.map_uid("A", "p")
            second = mapper.map_uid("B", "p")
            mapper.save()

            reloaded = UidMapper.load(path)
            self.assertEqual(reloaded.map_uid("A", "p"), first)
            self.assertEqual(reloaded.map_uid("B", "p"), second)
            self.assertNotEqual(first, second)
            self.assertEqual(reloaded.resolve(first, "p"), "A")
            self.assertEqual(reloaded.resolve(second, "p"), "B")

    def test_long_base_root_still_emits_legal_short_uids(self):
        long_root = "1.2.840.10008.999999.123456789.1234567890"
        self.assertTrue(is_legal_uid(long_root))
        mapper = UidMapper(base_root=long_root)
        uid = mapper.map_uid("x", "p")
        self.assertTrue(is_legal_uid(uid), uid)
        self.assertLessEqual(len(uid), 64)

    def test_extreme_base_root_forces_legal_compact_fallback(self):
        # 61 character legal root; root + ".1.1" would be 65 chars, so
        # every composed candidate is illegal and the mapper must use
        # the compact 2.25.<integer> fallback instead of emitting an
        # overlong / illegal UID.
        extreme_root = "1.2.3." + "1" * 55
        self.assertEqual(len(extreme_root), 61)
        self.assertTrue(is_legal_uid(extreme_root))
        mapper = UidMapper(base_root=extreme_root)
        first = mapper.map_uid("x", "p")
        second = mapper.map_uid("y", "p")
        self.assertTrue(is_legal_uid(first), first)
        self.assertTrue(is_legal_uid(second), second)
        self.assertLessEqual(len(first), 64)
        self.assertNotEqual(first, second)
        # stability after the fallback allocation
        self.assertEqual(mapper.map_uid("x", "p"), first)

    def test_idempotent_walking_of_already_pseudonymized_value(self):
        mapper = UidMapper(base_root="1.2.3.4")
        pseudo = mapper.map_uid("orig", "p")
        # Feeding the pseudonym back in the same project must not double map.
        self.assertEqual(mapper.map_uid(pseudo, "p"), pseudo)


class InMemoryThreadSafetyTests(unittest.TestCase):
    def test_rapid_mapping_under_lock_remains_bijective(self):
        mapper = UidMapper(base_root="1.2.3.4")
        errors = []

        def worker(t):
            try:
                for i in range(200):
                    mapper.map_uid(f"uid-{t}-{i}", "p")
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        table = mapper.mapping_for("p")
        self.assertEqual(len(table), 1600)
        self.assertEqual(len(set(table.values())), 1600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
