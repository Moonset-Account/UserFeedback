import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deid_gateway import FieldDictionary, process_tree  # noqa: E402
from deid_gateway.dictionary import is_private_tag, normalize_tag  # noqa: E402


class TagNormalizationTests(unittest.TestCase):
    def test_normalize_common_tag_forms(self):
        for form in ("0020000D", "0020000d", "0020,000D", "(0020,000D)", "0020 000D"):
            self.assertEqual(normalize_tag(form), "0020000D", form)
        self.assertIsNone(normalize_tag("StudyInstanceUID"))
        self.assertIsNone(normalize_tag("not-a-tag"))

    def test_odd_group_is_private(self):
        self.assertTrue(is_private_tag("00191001"))
        self.assertTrue(is_private_tag("00090010"))
        self.assertFalse(is_private_tag("0020000D"))
        self.assertFalse(is_private_tag("00081115"))


class DictionaryShapeTests(unittest.TestCase):
    def test_flat_vr_string_dictionary(self):
        dictionary = FieldDictionary.from_dict(
            {"StudyInstanceUID": "UID", "TransferSyntaxUID": "UI"}
        )
        entry = dictionary.lookup("study_instance_uid")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.vr, "UI")
        self.assertTrue(entry.is_uid)

    def test_fields_object_and_list_shapes(self):
        d1 = FieldDictionary.from_dict(
            {"fields": {"0020000D": {"type": "UID", "role": "id"}}}
        )
        self.assertEqual(d1.lookup("0020000D").vr, "UI")

        d2 = FieldDictionary.from_dict(
            [{"tag": "0020000D", "type": "UID", "role": "id"}]
        )
        self.assertEqual(d2.lookup("0020000D").vr, "UI")

    def test_approval_list_forms(self):
        dictionary = FieldDictionary.from_dict(
            {
                "fields": {"00191020": {"vr": "LO", "approved": True}},
                "approved_private": ["PrivateSiteCode", "0019,10AA"],
            }
        )
        self.assertTrue(dictionary.is_approved_private("00191020"))
        self.assertTrue(dictionary.is_approved_private("PrivateSiteCode"))
        self.assertTrue(dictionary.is_approved_private("001910AA"))


class PrivateAttributeRemovalTests(unittest.TestCase):
    DICTIONARY = {
        "fields": {
            "00191020": {"keyword": "ApprovedSiteCode", "vr": "LO", "approved": True},
            "0020000D": {"keyword": "StudyInstanceUID", "vr": "UID"},
        },
        "approved_private": ["00191020"],
    }

    def test_unapproved_private_tag_and_content_removed(self):
        tree = {
            "00190010": {"vr": "LO", "Value": ["VENDOR"]},
            "00191001": {
                "vr": "LO",
                "Value": ["serial-12345", "owner-info"],
            },
            "0020000D": {"vr": "UI", "Value": ["1.2.3.study"]},
        }
        result = process_tree(tree, self.DICTIONARY)
        self.assertNotIn("00190010", result)
        self.assertNotIn("00191001", result)
        # content cannot survive elsewhere
        text = repr(result)
        self.assertNotIn("serial-12345", text)
        self.assertNotIn("owner-info", text)
        self.assertNotIn("VENDOR", text)
        self.assertIn("0020000D", result)

    def test_approved_private_attribute_retained(self):
        tree = {"00191020": {"vr": "LO", "Value": ["site-42"]}}
        result = process_tree(tree, self.DICTIONARY)
        self.assertIn("00191020", result)
        self.assertEqual(result["00191020"]["Value"], ["site-42"])

    def test_private_removal_is_recursive_inside_sequences(self):
        tree = {
            "00081115": {
                "vr": "SQ",
                "Value": [
                    {
                        "0020000E": {"vr": "UI", "Value": ["1.2.3.series"]},
                        "00191001": {"vr": "LO", "Value": ["hidden-deep"]},
                        "00081140": {
                            "vr": "SQ",
                            "Value": [
                                {"00191009": {"vr": "LO", "Value": ["deeper-secret"]}}
                            ]
                        },
                    }
                ],
            }
        }
        result = process_tree(tree, self.DICTIONARY)
        item = result["00081115"]["Value"][0]
        self.assertNotIn("00191001", item)
        inner = item["00081140"]["Value"][0]
        self.assertNotIn("00191009", inner)
        self.assertNotIn("deeper-secret", repr(result))
        self.assertIn("0020000E", item)

    def test_explicit_private_keyword_without_approval_removed(self):
        dictionary = {
            "fields": {
                "SecretVendorField": {"vr": "LO", "private": True},
                "ApprovedVendorField": {"vr": "LO", "private": True, "approved": True},
            }
        }
        tree = {
            "SecretVendorField": "do-not-keep",
            "ApprovedVendorField": "keep-me",
            "StudyInstanceUID": "1.2.3.study",
        }
        result = process_tree(tree, dictionary)
        self.assertNotIn("SecretVendorField", result)
        self.assertNotIn("do-not-keep", repr(result))
        self.assertIn("ApprovedVendorField", result)
        self.assertEqual(result["ApprovedVendorField"], "keep-me")

    def test_original_tree_is_not_mutated(self):
        import copy

        tree = {"00191001": {"vr": "LO", "Value": ["x"]},
                "StudyInstanceUID": "1.2.3.s"}
        snapshot = copy.deepcopy(tree)
        process_tree(tree, self.DICTIONARY)
        self.assertEqual(tree, snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
