import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deid_gateway import (  # noqa: E402
    DeidGateway,
    ProcessReport,
    UidMapper,
    is_legal_uid,
    process_tree,
)

DICTIONARY = {
    "fields": {
        "TransferSyntaxUID": {"tag": "00020010", "vr": "UID", "role": "encoding_constant"},
        "StudyInstanceUID": {"tag": "0020000D", "vr": "UID"},
        "SeriesInstanceUID": {"tag": "0020000E", "vr": "UID"},
        "SOPInstanceUID": {"tag": "00080018", "vr": "UID"},
        "ReferencedSOPInstanceUID": {"tag": "00081155", "vr": "UID", "role": "reference"},
        "ReferencedImageSequence": {"tag": "00081140", "vr": "SQ"},
    },
    "registered_constants": ["9.9.9.encoding.constant"],
}


class StandardJsonShapeTests(unittest.TestCase):
    def test_every_ui_value_is_replaced_with_legal_uid_and_shared(self):
        tree = {
            "0020000D": {"vr": "UI", "Value": ["1.2.3.study"]},
            "00080018": {"vr": "UI", "Value": ["1.2.3.sop"]},
            "00081140": {
                "vr": "SQ",
                "Value": [
                    {"00081155": {"vr": "UI", "Value": ["1.2.3.sop"]}},
                    {"00081155": {"vr": "UI", "Value": ["1.2.3.sop2"]}},
                ],
            },
        }
        mapper = UidMapper(base_root="1.2.8.8")
        result = process_tree(tree, DICTIONARY, mapper=mapper, project="p")
        study = result["0020000D"]["Value"][0]
        sop = result["00080018"]["Value"][0]
        ref1 = result["00081140"]["Value"][0]["00081155"]["Value"][0]
        ref2 = result["00081140"]["Value"][1]["00081155"]["Value"][0]
        for uid in (study, sop, ref1, ref2):
            self.assertTrue(is_legal_uid(uid), uid)
        self.assertEqual(sop, ref1)          # identifier == its reference
        self.assertNotEqual(sop, ref2)
        self.assertNotIn("1.2.3.sop", json.dumps(result))

    def test_uid_multiplicity_list_is_mapped_elementwise(self):
        tree = {"StudyInstanceUID": {"vr": "UI", "Value": ["u1", "u2", "u3"]}}
        result = process_tree(tree, DICTIONARY, UidMapper(base_root="1.2.3.4"))
        values = result["StudyInstanceUID"]["Value"]
        self.assertEqual(len(values), 3)
        self.assertEqual(len(set(values)), 3)
        self.assertTrue(all(is_legal_uid(v) for v in values))

    def test_empty_and_missing_values_pass_through(self):
        tree = {
            "StudyInstanceUID": {"vr": "UI", "Value": []},
            "SeriesInstanceUID": {"vr": "UI"},
            "SOPInstanceUID": None,
        }
        result = process_tree(tree, DICTIONARY)
        self.assertEqual(result["StudyInstanceUID"]["Value"], [])
        self.assertEqual(result["SeriesInstanceUID"], {"vr": "UI"})
        self.assertIsNone(result["SOPInstanceUID"])


class ConstantPreservationTests(unittest.TestCase):
    def test_builtin_transfer_syntax_registry_kept(self):
        for ts in ("1.2.840.10008.1.2", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.4.50"):
            tree = {"TransferSyntaxUID": {"vr": "UI", "Value": [ts]}}
            result = process_tree(tree, DICTIONARY)
            self.assertEqual(result["TransferSyntaxUID"]["Value"][0], ts)

    def test_dictionary_role_constant_field_kept(self):
        tree = {"00020010": {"vr": "UI", "Value": ["7.7.7.not.in.builtin.registry"]}}
        result = process_tree(tree, DICTIONARY)
        self.assertEqual(
            result["00020010"]["Value"][0], "7.7.7.not.in.builtin.registry"
        )

    def test_dictionary_listed_registered_constant_value_kept(self):
        tree = {"AnyUID": {"vr": "UI", "Value": ["9.9.9.encoding.constant"]}}
        result = process_tree(tree, DICTIONARY)
        self.assertEqual(result["AnyUID"]["Value"][0], "9.9.9.encoding.constant")

    def test_constants_are_not_entered_in_mapping(self):
        mapper = UidMapper(base_root="1.2.3.4")
        tree = {"TransferSyntaxUID": "1.2.840.10008.1.2.1"}
        process_tree(tree, DICTIONARY, mapper=mapper, project="p")
        self.assertEqual(mapper.mapping_for("p"), {})


class SimplifiedShapeTests(unittest.TestCase):
    def test_bare_keyword_scalars_and_lists(self):
        tree = {
            "StudyInstanceUID": "1.2.3.study",
            "ReferencedSOPInstanceUID": ["1.2.3.a", "1.2.3.b"],
            "ReferencedImageSequence": [
                {"ReferencedSOPInstanceUID": "1.2.3.a"}
            ],
        }
        mapper = UidMapper(base_root="1.2.3.4")
        result = process_tree(tree, DICTIONARY, mapper=mapper, project="p")
        self.assertTrue(is_legal_uid(result["StudyInstanceUID"]))
        self.assertEqual(len(result["ReferencedSOPInstanceUID"]), 2)
        nested = result["ReferencedImageSequence"][0]["ReferencedSOPInstanceUID"]
        self.assertEqual(nested, result["ReferencedSOPInstanceUID"][0])

    def test_camel_case_keys_match_dictionary(self):
        tree = {"studyInstanceUID": "1.2.3.s",
                "reviews": [{"referencedSOPInstanceUID": "1.2.3.o"}]}
        result = process_tree(tree, DICTIONARY, UidMapper(base_root="1.2.3.4"))
        self.assertTrue(is_legal_uid(result["studyInstanceUID"]))
        self.assertTrue(is_legal_uid(result["reviews"][0]["referencedSOPInstanceUID"]))

    def test_guid_like_key_is_not_name_matched(self):
        tree = {"guid": "aaaaaaaa-bbbb-cccc", "outer": {"GUID": "x"}}
        result = process_tree(tree, DICTIONARY)
        self.assertEqual(result["guid"], "aaaaaaaa-bbbb-cccc")
        self.assertEqual(result["outer"]["GUID"], "x")

    def test_explicit_node_shape_with_type_value_items(self):
        tree = {
            "root": {
                "tag": "0020000D",
                "type": "UID",
                "value": "1.2.3.study",
            },
            "seq": {
                "type": "Sequence",
                "items": [{"SOPInstanceUID": "1.2.3.sop"}],
            },
        }
        result = process_tree(tree, DICTIONARY, UidMapper(base_root="1.2.3.4"))
        self.assertTrue(is_legal_uid(result["root"]["value"]))
        self.assertTrue(is_legal_uid(result["seq"]["items"][0]["SOPInstanceUID"]))


class JsonAndReportTests(unittest.TestCase):
    def test_gateway_process_json_roundtrip(self):
        gateway = DeidGateway(DICTIONARY, base_root="1.2.3.4", project="p")
        text = json.dumps({"StudyInstanceUID": "1.2.3.s"})
        out_text = gateway.process_json(text)
        parsed = json.loads(out_text)
        self.assertTrue(is_legal_uid(parsed["StudyInstanceUID"]))

    def test_report_records_replacements_constants_and_removals(self):
        report = ProcessReport()
        tree = {
            "StudyInstanceUID": "1.2.3.s",
            "TransferSyntaxUID": "1.2.840.10008.1.2",
            "00191001": {"vr": "LO", "Value": ["secret"]},
        }
        process_tree(tree, DICTIONARY, project="p", report=report)
        counts = report.counts()
        self.assertEqual(counts.get("uid_replaced"), 1)
        self.assertGreaterEqual(counts.get("constant_kept", 0), 1)
        self.assertEqual(counts.get("private_removed"), 1)
        self.assertTrue(report.replaced)
        self.assertTrue(report.private_removed)

    def test_input_deep_structure_unchanged_after_processing(self):
        tree = {"StudyInstanceUID": "1.2.3.s",
                "seq": [{"ReferencedSOPInstanceUID": "1.2.3.o"}]}
        original = json.dumps(tree, sort_keys=True)
        gateway = DeidGateway(DICTIONARY, base_root="1.2.3.4")
        gateway.process(tree, "p")
        self.assertEqual(json.dumps(tree, sort_keys=True), original)

    def test_reprocessing_output_is_stable_and_not_double_mapped(self):
        gateway = DeidGateway(DICTIONARY, base_root="1.2.3.4", project="p")
        tree = {
            "StudyInstanceUID": "1.2.3.s",
            "ReferencedImageSequence": [
                {"ReferencedSOPInstanceUID": "1.2.3.o"}
            ],
        }
        first = gateway.process(tree)
        second = gateway.process(first)
        self.assertEqual(first, second)
        study = first["StudyInstanceUID"]
        self.assertTrue(is_legal_uid(study))
        # the pseudonym is not wrapped into a longer double mapping
        self.assertEqual(second["StudyInstanceUID"], study)


if __name__ == "__main__":
    unittest.main(verbosity=2)
