"""模型校验与版本化状态重建的单元测试。"""

import unittest

from sigcheck.model import (
    BODY,
    FIELD,
    DocumentModelError,
    load_document,
)

from tests.builders import DocBuilder, standard_revisions


def minimal_builder() -> DocBuilder:
    builder = DocBuilder(standard_revisions(3))
    (
        builder.set_page_roots("page1")
        .add_object("page1", "r0", "d-page1")
        .add_object("content1", "r0", "d-content1")
        .add_body_edge("page1", "content1", "r0")
        .add_object("sigobj1", "r1", "d-sig")
        .add_signature("sig1", "r1", (100, 140), "sigobj1")
    )
    return builder


class ModelValidationTest(unittest.TestCase):
    def test_minimal_valid_model_builds(self) -> None:
        doc = minimal_builder().build()
        self.assertEqual(doc.revision_index, {"r0": 0, "r1": 1, "r2": 2})

    def test_load_from_dict_roundtrip(self) -> None:
        doc = minimal_builder().build_via_dict()
        edges = doc.active_edges(2)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].kind, BODY)
        self.assertEqual(edges[0].target_id, "content1")

    def test_load_from_json_string(self) -> None:
        import json

        payload = {
            "revisions": [
                {"revision_id": "r0", "start": 0, "end": 100},
                {"revision_id": "r1", "start": 100, "end": 200},
            ],
            "objects": [
                {"object_id": "p", "revision_id": "r0", "digest": "dp"},
                {"object_id": "c", "revision_id": "r0", "digest": "dc"},
                {"object_id": "sig", "revision_id": "r1", "digest": "ds"},
            ],
            "references": [
                {
                    "source_id": "p",
                    "target_id": "c",
                    "kind": BODY,
                    "added_in": "r0",
                    "removed_in": None,
                    "field_name": None,
                }
            ],
            "signatures": [
                {
                    "signature_id": "sig1",
                    "revision_id": "r1",
                    "byte_range": [100, 150],
                    "signature_object_id": "sig",
                }
            ],
            "page_root_ids": ["p"],
        }
        rebuilt = load_document(json.dumps(payload))
        self.assertEqual(
            [r.revision_id for r in rebuilt.revisions], ["r0", "r1"]
        )
        self.assertEqual(rebuilt.active_edges(1)[0].target_id, "c")

    def test_empty_revisions_rejected(self) -> None:
        with self.assertRaises(DocumentModelError):
            DocBuilder([]).set_page_roots().build()

    def test_duplicate_revision_id_rejected(self) -> None:
        builder = DocBuilder(
            [("r0", 0, 10), ("r0", 10, 20)]
        ).set_page_roots("p")
        builder.add_object("p", "r0", "d")
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_overlapping_revision_ranges_rejected(self) -> None:
        builder = DocBuilder(
            [("r0", 0, 100), ("r1", 90, 190)]
        ).set_page_roots("p")
        builder.add_object("p", "r0", "d")
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_unsorted_revisions_rejected(self) -> None:
        builder = DocBuilder(
            [("r1", 100, 200), ("r0", 0, 100)]
        ).set_page_roots("p")
        builder.add_object("p", "r0", "d")
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_unknown_revision_in_object_rejected(self) -> None:
        builder = DocBuilder(standard_revisions(2)).set_page_roots("p")
        builder.add_object("p", "r0", "d").add_object("x", "r9", "d")
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_field_edge_without_field_name_rejected(self) -> None:
        from sigcheck.model import ReferenceEdge

        doc_builder = DocBuilder(standard_revisions(2)).set_page_roots("p")
        doc_builder.add_object("p", "r0", "d").add_object("v", "r0", "v")
        doc_builder.edges.append(
            ReferenceEdge(
                source_id="p",
                target_id="v",
                kind=FIELD,
                added_in="r0",
            )
        )
        with self.assertRaises(DocumentModelError):
            doc_builder.build()

    def test_unknown_edge_kind_rejected(self) -> None:
        from sigcheck.model import ReferenceEdge

        doc_builder = DocBuilder(standard_revisions(2)).set_page_roots("p")
        doc_builder.add_object("p", "r0", "d").add_object("v", "r0", "v")
        doc_builder.edges.append(
            ReferenceEdge(
                source_id="p",
                target_id="v",
                kind="shadow",
                added_in="r0",
            )
        )
        with self.assertRaises(DocumentModelError):
            doc_builder.build()

    def test_removed_before_added_rejected(self) -> None:
        builder = (
            DocBuilder(standard_revisions(3))
            .set_page_roots("p")
            .add_object("p", "r0", "d")
            .add_object("v", "r0", "v")
            .add_field_edge("p", "v", "f1", added_in="r1", removed_in="r0")
        )
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_approval_signature_with_allowed_fields_rejected(self) -> None:
        builder = (
            DocBuilder(standard_revisions(2))
            .set_page_roots("p")
            .add_object("p", "r0", "d")
            .add_object("sig", "r1", "s")
            .add_signature(
                "sig1",
                "r1",
                (100, 130),
                "sig",
                sig_type="approval",
                allowed_fields=("f1",),
            )
        )
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_signature_range_outside_its_revision_rejected(self) -> None:
        builder = (
            DocBuilder(standard_revisions(3))
            .set_page_roots("p")
            .add_object("p", "r0", "d")
            .add_object("sig", "r1", "s")
            # (250,260) 落在 r2 的全局区间，不属于签章所在的 r1[100,200)。
            .add_signature("sig1", "r1", (250, 260), "sig")
        )
        with self.assertRaises(DocumentModelError):
            builder.build()

    def test_unknown_page_root_rejected(self) -> None:
        builder = DocBuilder(standard_revisions(2)).set_page_roots("ghost")
        builder.add_object("p", "r0", "d")
        with self.assertRaises(DocumentModelError):
            builder.build()


class VersionedStateTest(unittest.TestCase):
    def test_object_versions_tracking(self) -> None:
        builder = (
            DocBuilder(standard_revisions(4))
            .set_page_roots("p")
            .add_object("p", "r0", "p0")
            .add_object("c", "r0", "c0")
            .add_object("c", "r2", "c1")
        )
        doc = builder.build()
        self.assertIsNone(doc.version_at("x", 0))
        self.assertEqual(doc.version_at("c", 0).digest, "c0")
        self.assertEqual(doc.version_at("c", 1).digest, "c0")
        self.assertEqual(doc.version_at("c", 2).digest, "c1")
        self.assertEqual(doc.version_at("c", 3).digest, "c1")

    def test_edge_lifecycle_add_and_remove(self) -> None:
        builder = (
            DocBuilder(standard_revisions(4))
            .set_page_roots("p")
            .add_object("p", "r0", "p0")
            .add_object("c", "r0", "c0")
            .add_body_edge("p", "c", "r0", removed_in="r2")
        )
        doc = builder.build()
        self.assertEqual(len(doc.active_edges(0)), 1)
        self.assertEqual(len(doc.active_edges(1)), 1)
        self.assertEqual(doc.active_edges(2), [])
        self.assertEqual(doc.active_edges(3), [])
        events = doc.edge_events_at(2)
        self.assertEqual(events[0][0], "remove")

    def test_same_object_multiple_events_in_one_revision_rejected(self) -> None:
        builder = (
            DocBuilder(standard_revisions(2))
            .set_page_roots("p")
            .add_object("p", "r0", "d")
            .add_object("c", "r1", "a")
            .add_object("c", "r1", "b")
        )
        with self.assertRaises(DocumentModelError):
            builder.build()


if __name__ == "__main__":
    unittest.main()
