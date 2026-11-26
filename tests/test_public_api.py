import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import deid_gateway  # noqa: E402
import gateway as gateway_shim  # noqa: E402
import mapper as mapper_shim  # noqa: E402
import uid_mapper as uid_mapper_shim  # noqa: E402
from deid_gateway import (  # noqa: E402
    DeidGateway,
    ShortCodeGenerator,
    UidMapper,
    deidentify,
    is_legal_uid,
    process_tree,
)


class PublicApiTests(unittest.TestCase):
    def test_package_exports_core_symbols(self):
        for name in (
            "DeidGateway", "UidMapper", "FieldDictionary", "process_tree",
            "ProcessReport", "ShortCodeGenerator",
            "ScriptedShortCodeGenerator", "is_legal_uid",
            "MappingConflictError", "deidentify",
        ):
            self.assertTrue(hasattr(deid_gateway, name), name)

    def test_root_compatibility_shims_reexport_same_classes(self):
        self.assertIs(gateway_shim.DeidGateway, DeidGateway)
        self.assertIs(uid_mapper_shim.UidMapper, deid_gateway.UidMapper)
        self.assertIs(mapper_shim.UidMapper, deid_gateway.UidMapper)
        self.assertTrue(callable(gateway_shim.process_tree))

    def test_deidentify_one_shot(self):
        result = deidentify(
            {"StudyInstanceUID": "1.2.3.s"},
            {"StudyInstanceUID": "UID"},
            project="p",
            base_root="1.2.3.4",
        )
        self.assertTrue(is_legal_uid(result["StudyInstanceUID"]))

    def test_json_document_and_json_dictionary_strings(self):
        dictionary_text = json.dumps({"StudyInstanceUID": {"type": "UID"}})
        tree_text = json.dumps({"StudyInstanceUID": "1.2.3.s"})
        result = process_tree(tree_text, dictionary_text, project="p")
        self.assertTrue(is_legal_uid(result["StudyInstanceUID"]))

        gw = DeidGateway(dictionary=dictionary_text, base_root="1.2.3.4")
        result2 = gw.process(tree_text, "p")
        self.assertTrue(is_legal_uid(result2["StudyInstanceUID"]))

    def test_explicit_private_nodes_inside_list_are_removed(self):
        dictionary = {
            "fields": {
                "00191020": {"vr": "LO", "private": True, "approved": True}
            }
        }
        tree = {
            "items": [
                {"tag": "00191001", "vr": "LO", "value": "hidden-node"},
                {"tag": "00191020", "vr": "LO", "value": "approved-node"},
                {"tag": "0020000D", "vr": "UID", "value": "1.2.3.s"},
            ]
        }
        result = process_tree(tree, dictionary)
        values = [item.get("tag") for item in result["items"]]
        self.assertNotIn("00191001", values)
        self.assertIn("00191020", values)
        self.assertIn("0020000D", values)
        self.assertNotIn("hidden-node", json.dumps(result))

    def test_process_tree_accepts_json_array_root(self):
        result = process_tree(
            '[{"StudyInstanceUID": "1.2.3.s"}]',
            {"StudyInstanceUID": "UID"},
        )
        self.assertIsInstance(result, list)
        self.assertTrue(is_legal_uid(result[0]["StudyInstanceUID"]))


class CallingConventionCompatibilityTests(unittest.TestCase):
    def test_uid_mapper_positional_root_string(self):
        mapper = UidMapper("1.2.3.4")
        uid = mapper.map_uid("u", "p")
        self.assertTrue(uid.startswith("1.2.3.4."), uid)

    def test_uid_mapper_positional_json_path_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "mapping.json")
            mapper = UidMapper(target, "1.2.3.4")
            uid = mapper.map_uid("u", "p")
            self.assertTrue(Path(target).exists())
            reloaded = UidMapper.load(target)
            self.assertEqual(reloaded.map_uid("u", "p"), uid)

    def test_gateway_accepts_mapper_as_second_positional(self):
        mapper = UidMapper(base_root="1.2.3.4")
        gw = DeidGateway({"StudyInstanceUID": "UID"}, mapper)
        out = gw.process({"StudyInstanceUID": "s"}, "p")
        self.assertTrue(out["StudyInstanceUID"].startswith("1.2.3.4."))

    def test_generator_property_injection_and_method_aliases(self):
        mapper = UidMapper(base_root="1.2.3.4")

        class FakeGenerator(ShortCodeGenerator):
            def next_code(self, namespace="default"):
                return "42"

        mapper.generator = FakeGenerator()
        self.assertEqual(mapper.generator.generate("p"), "42")
        uid = mapper.map_uid("u", "p")
        self.assertEqual(uid, "1.2.3.4.1.42")

    def test_generator_aliases_exported(self):
        self.assertIs(deid_gateway.UidGenerator, ShortCodeGenerator)
        self.assertTrue(hasattr(uid_mapper_shim, "UidGenerator"))

    def test_generator_kwarg_and_alternative_method_names(self):
        class NextIdOnly:
            def __init__(self):
                self.values = iter(["3", "3", "4"])

            def next_id(self, namespace):
                return next(self.values)

        mapper = UidMapper(base_root="1.2.3.4", generator=NextIdOnly())
        a = mapper.map_uid("A", "p")
        b = mapper.map_uid("B", "p")
        self.assertEqual(a, "1.2.3.4.1.3")
        self.assertNotEqual(b, a)
        self.assertEqual(mapper.map_uid("A", "p"), a)
        self.assertEqual(mapper.resolve(a, "p"), "A")

    def test_callable_generator_injection(self):
        answers = iter(["5", "5", "6"])

        def generate(project):
            return next(answers)

        mapper = UidMapper(base_root="1.2.3.4", code_generator=generate)
        a = mapper.map_uid("A", "p")
        b = mapper.map_uid("B", "p")
        self.assertEqual(a, "1.2.3.4.1.5")
        self.assertEqual(b, "1.2.3.4.1.6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
