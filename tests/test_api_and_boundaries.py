import json
import unittest

from safe_dump_planner import (
    Crop,
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    FieldSpec,
    PageSpec,
    PointerRef,
    RecordSpec,
    ValidationError,
    VirtualAlias,
    materialize_dump,
    plan_safe_dump,
)


class RecordsRolesAndCropsTests(unittest.TestCase):
    def test_stack_frame_and_allocation_record_fields_are_planned(self):
        request = DumpRequest(
            pages=[
                PageSpec(
                    "stack-page",
                    size=128,
                    role="stack",
                    records=[
                        RecordSpec(
                            "frame-1",
                            offset=0,
                            size=32,
                            kind="stack_frame",
                            fields=[FieldSpec("saved-rbp", 0, 8), FieldSpec("return-pc", 8, 8)],
                        )
                    ],
                ),
                PageSpec(
                    "alloc-page",
                    size=128,
                    role="allocation",
                    records=[
                        RecordSpec(
                            "alloc-1",
                            offset=32,
                            size=48,
                            kind="allocation",
                            fields=[FieldSpec("next", 32, 8), FieldSpec("size", 40, 8)],
                        )
                    ],
                ),
            ],
            aliases=[VirtualAlias("s", "stack-page", domain="d"), VirtualAlias("a", "alloc-page", domain="d")],
            policies=[DomainPolicy.full("d", addresses=True)],
            pointers=[
                PointerRef(page_id="stack-page", offset=8, target_page="alloc-page"),
            ],
            targets=[
                DiagnosticTarget("frame", page_id="stack-page", record_id="frame-1", field_names=["return-pc"]),
                DiagnosticTarget("alloc-size", page_id="alloc-page", record_id="alloc-1", field_names=["size"]),
            ],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.pages["stack-page"].role, "stack")
        self.assertEqual(plan.pages["alloc-page"].role, "allocation")
        self.assertEqual(
            [(f.page_id, f.start, f.end) for f in plan.fragments],
            [("stack-page", 8, 16), ("alloc-page", 40, 48)],
        )

    def test_optional_crop_is_an_allowlist_and_explains_removed_bytes(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=128)],
            aliases=[VirtualAlias("a", "p", domain="d")],
            policies=[DomainPolicy.full("d")],
            crops=[Crop(page_id="p", ranges=[(0, 32)])],
            targets=[DiagnosticTarget("too-wide", page_id="p", ranges=[(0, 64)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.targets[0].status, "cropped")
        self.assertEqual(plan.targets[0].reasons[0].code, "CROPPED_BY_DUMPER")
        self.assertEqual(plan.targets[0].reasons[0].range.to_dict(), [32, 64])

        narrow = DumpRequest(
            pages=request.pages,
            aliases=request.aliases,
            policies=request.policies,
            crops=request.crops,
            targets=[DiagnosticTarget("narrow", page_id="p", ranges=[(0, 32)])],
        )
        self.assertEqual(plan_safe_dump(narrow).targets[0].status, "retained")


class BoundaryAndApiTests(unittest.TestCase):
    def test_end_exactly_page_size_is_valid(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=64)],
            aliases=[VirtualAlias("a", "p", domain="d")],
            policies=[DomainPolicy.full("d")],
            targets=[DiagnosticTarget("edge", page_id="p", ranges=[(0, 64)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.fragments[0].end, 64)

    def test_target_range_past_page_end_returns_explainable_invalid_selector(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=64)],
            aliases=[VirtualAlias("a", "p", domain="d")],
            policies=[DomainPolicy.full("d")],
            targets=[DiagnosticTarget("past", page_id="p", ranges=[(0, 65)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.targets[0].status, "invalid_selector")
        self.assertEqual(plan.targets[0].reasons[0].code, "INVALID_SELECTOR")

    def test_zero_length_only_target_is_invalid_selector(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=64)],
            aliases=[VirtualAlias("a", "p", domain="d")],
            policies=[DomainPolicy.full("d")],
            targets=[DiagnosticTarget("zero", page_id="p", ranges=[(10, 10)])],
        )
        plan = plan_safe_dump(request)
        self.assertEqual(plan.targets[0].status, "invalid_selector")

    def test_pointer_outside_page_is_strict_input_error(self):
        request = DumpRequest(
            pages=[PageSpec("p", size=64)],
            pointers=[PointerRef(page_id="p", offset=60, size=8)],
        )
        with self.assertRaises(ValidationError):
            plan_safe_dump(request)

    def test_dict_request_json_plan_and_safe_manifest(self):
        restricted_addr = 0x1234_5678_9ABC_DEF0
        request_dict = {
            "pages": [
                {
                    "page_id": "p",
                    "size": 16,
                    "data_hex": restricted_addr.to_bytes(8, "little").hex() + "4142434445464748",
                    "physical_address": 0x1111_2222_3333_4444,
                },
                {"page_id": "secret", "size": 64, "physical_address": restricted_addr},
            ],
            "aliases": [
                {"alias_id": "a", "page_id": "p", "domain": "d", "virtual_address": 0x5555},
                {"alias_id": "sa", "page_id": "secret", "domain": "d", "virtual_address": restricted_addr},
            ],
            "policies": [{"domain": "d", "allow_all_content": True, "allow_all_addresses": False}],
            "targets": [{"target_id": "t", "priority": 2, "page_id": "p", "ranges": [[0, 16]]}],
            "budget_bytes": 64,
        }
        plan = plan_safe_dump(request_dict)
        # The full plan is JSON-serializable.
        json.dumps(plan.to_dict())
        artifact = materialize_dump(request_dict, plan)
        manifest_text = json.dumps(artifact.manifest)
        full_artifact_text = json.dumps(artifact.to_dict())
        self.assertNotIn(str(restricted_addr), manifest_text)
        self.assertNotIn(hex(restricted_addr), manifest_text)
        self.assertNotIn(hex(restricted_addr), full_artifact_text)
        self.assertNotIn("page_id", full_artifact_text)
        self.assertNotIn("virtual_address", manifest_text)
        self.assertTrue(manifest_text.startswith("{"))
        self.assertEqual(artifact.verify_no_restricted_addresses(DumpRequest.from_dict(request_dict)), [])
        self.assertIn("target t", plan.explain())

    def test_duplicate_page_ids_raise(self):
        with self.assertRaises(ValidationError):
            plan_safe_dump(
                DumpRequest(
                    pages=[PageSpec("p", size=8), PageSpec("p", size=8)],
                )
            )


if __name__ == "__main__":
    unittest.main()
