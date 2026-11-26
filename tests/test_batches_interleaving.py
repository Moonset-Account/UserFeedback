import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

from deid_gateway import DeidGateway, UidMapper, is_legal_uid  # noqa: E402


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


DICTIONARY_PATH = FIXTURES / "field_dictionary.json"

STUDY = "1.2.826.0.1.3680043.9.7195.100"
SERIES = "1.2.826.0.1.3680043.9.7195.200"
FRAME = "1.2.826.0.1.3680043.9.7195.250"
IMG_SOP_1 = "1.2.826.0.1.3680043.9.7195.3001"
IMG_SOP_2 = "1.2.826.0.1.3680043.9.7195.3002"
ANN_SOP = "1.2.826.0.1.3680043.9.7195.4001"
TS = "1.2.840.10008.1.2.1"


def annotation_image_refs(ann):
    series_item = ann["00081115"]["Value"][0]
    return [
        item["00081155"]["Value"][0]
        for item in series_item["00081140"]["Value"]
    ]


class InterleavedBatchAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
        self.image = load_fixture("image_export.json")
        self.annotation = load_fixture("annotation_export.json")
        self.checklist = load_fixture("checklist_export.json")

    def _assert_all_pseudonyms_legal(self, *trees):
        for tree in trees:
            for value in self._iter_gateway_uids(tree):
                self.assertTrue(is_legal_uid(value), value)

    def _iter_gateway_uids(self, obj):
        if isinstance(obj, dict):
            for value in obj.values():
                yield from self._iter_gateway_uids(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from self._iter_gateway_uids(value)
        elif isinstance(obj, str) and obj.startswith("1.2.917195"):
            yield obj

    def test_annotation_arriving_before_image_still_aligns_all_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping_path = Path(tmp) / "uid-map.json"
            gateway = DeidGateway(
                self.dictionary,
                mapping_path=mapping_path,
                project="receiving-hospital-x",
                base_root="1.2.917195",
            )

            # 1) annotation arrives FIRST and mints the shared pseudonyms.
            ann = gateway.export(self.annotation, batch="annotation").tree
            ann_study = ann["0020000D"]["Value"][0]
            ann_series = ann["00081115"]["Value"][0]["0020000E"]["Value"][0]
            ann_sop = ann["00080018"]["Value"][0]
            ann_refs = annotation_image_refs(ann)

            # 2) image arrives later using the same shared mapping.
            img = gateway.export(self.image, batch="image").tree
            img_study = img["0020000D"]["Value"][0]
            img_series = img["0020000E"]["Value"][0]
            img_sop_1 = img["00080018"]["Value"][0]
            img_frame = img["00200052"]["Value"][0]

            # 3) review checklist arrives last.
            chk = gateway.export(self.checklist, batch="checklist").tree

            # study / series align across all three batches
            self.assertEqual(ann_study, img_study)
            self.assertEqual(chk["studyInstanceUID"], img_study)
            self.assertEqual(ann_series, img_series)
            self.assertEqual(chk["seriesInstanceUID"], img_series)
            self.assertEqual(chk["frameOfReferenceUID"], img_frame)

            # instance identifiers align with every reference
            self.assertEqual(img_sop_1, ann_refs[0])
            self.assertEqual(img_sop_1, chk["reviews"][0]["referencedSOPInstanceUID"])
            self.assertEqual(ann_refs[1], chk["reviews"][1]["referencedSOPInstanceUID"])
            self.assertEqual(
                ann_sop, chk["annotation"]["annotationSOPInstanceUID"]
            )

            # source UID values are gone from serialized outputs
            for source in (STUDY, SERIES, FRAME, IMG_SOP_1, IMG_SOP_2, ANN_SOP):
                self.assertNotIn(source, json.dumps((ann, img, chk)))

            # constants survive verbatim in every batch
            self.assertEqual(ann["00020010"]["Value"][0], TS)
            self.assertEqual(img["00020010"]["Value"][0], TS)
            self.assertEqual(chk["transferSyntaxUID"], TS)
            self.assertEqual(
                img["00080016"]["Value"][0],
                "1.2.840.10008.5.1.4.1.1.2",
            )

            # non UID look-alike is untouched
            self.assertEqual(
                chk["clientGuid"], "0c2f8c9a-1234-5678-9abc-def012345678"
            )

            # privacy clauses on the image batch
            for removed_tag in ("00190010", "00191001", "00191002"):
                self.assertNotIn(removed_tag, img)
            self.assertNotIn("SCANNER-SERIAL-SN-0001", json.dumps(img))
            self.assertIn("00191020", img)
            self.assertEqual(img["00191020"]["Value"], ["SITE-CODE-42"])

            self._assert_all_pseudonyms_legal(ann, img, chk)

    def test_image_first_order_produces_same_alignment(self):
        gateway = DeidGateway(self.dictionary, base_root="1.2.917195")
        img = gateway.process(self.image, "proj")
        ann = gateway.process(self.annotation, "proj")
        chk = gateway.process(self.checklist, "proj")
        self.assertEqual(
            img["0020000D"]["Value"][0], ann["0020000D"]["Value"][0]
        )
        self.assertEqual(
            img["0020000D"]["Value"][0], chk["studyInstanceUID"]
        )
        self.assertEqual(
            img["00080018"]["Value"][0],
            annotation_image_refs(ann)[0],
        )

    def test_new_gateway_loading_mapping_file_keeps_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "uid-map.json"

            first = DeidGateway(
                self.dictionary, mapping_path=path, base_root="1.2.917195"
            )
            ann_before = first.process(self.annotation, "proj")
            expected_study = ann_before["0020000D"]["Value"][0]
            expected_ann_sop = ann_before["00080018"]["Value"][0]
            first.save_mapping()
            self.assertTrue(path.exists())

            # Simulate a fresh process: brand new mapper loaded from disk.
            reloaded_mapper = UidMapper.load(path)
            second = DeidGateway(self.dictionary, mapper=reloaded_mapper)
            ann_after = second.process(self.annotation, "proj")
            self.assertEqual(
                ann_after["0020000D"]["Value"][0], expected_study
            )
            self.assertEqual(
                ann_after["00080018"]["Value"][0], expected_ann_sop
            )
            self.assertEqual(json.dumps(ann_after, sort_keys=True),
                             json.dumps(ann_before, sort_keys=True))

            img = second.process(self.image, "proj")
            self.assertEqual(img["0020000D"]["Value"][0], expected_study)

    def test_project_isolation_across_whole_batches(self):
        gateway = DeidGateway(self.dictionary, base_root="1.2.917195")
        a = gateway.process(self.annotation, "hospital-A")
        b = gateway.process(self.annotation, "hospital-B")
        self.assertNotEqual(
            a["0020000D"]["Value"][0], b["0020000D"]["Value"][0]
        )
        # constants remain identical regardless of project
        self.assertEqual(
            a["00020010"]["Value"][0], b["00020010"]["Value"][0]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
