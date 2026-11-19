"""Validation cases: malformed specifications must be refused loudly."""

from __future__ import annotations

import unittest

from release_check import InputError, parse_spec

from support import implementation, interface, method, spec_document


class SpecificationValidationTests(unittest.TestCase):
    def assert_rejected(self, document: dict, fragment: str) -> None:
        with self.assertRaises(InputError) as caught:
            parse_spec(document)
        self.assertIn(fragment, str(caught.exception))

    def test_unknown_top_level_key_is_refused(self) -> None:
        document = spec_document(["billing"], [])
        document["interface"] = []
        self.assert_rejected(document, "unknown key")

    def test_unknown_interface_key_is_refused(self) -> None:
        document = spec_document(["billing"], [])
        document["interfaces"] = [{"name": "IA", "public": True, "method": {}}]
        self.assert_rejected(document, "unknown key")

    def test_caller_set_is_required(self) -> None:
        self.assert_rejected({"interfaces": []}, "callers: required")

    def test_interfaces_section_is_required(self) -> None:
        self.assert_rejected({"callers": []}, "interfaces: required")

    def test_duplicate_caller_is_refused(self) -> None:
        self.assert_rejected(spec_document(["billing", "billing"], []), "duplicate entry")

    def test_duplicate_field_in_a_declaration_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA", methods={"generate": {"billing": ["id", "id"]}})],
        )
        self.assert_rejected(document, "duplicate field")

    def test_unknown_identity_in_a_declaration_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA", methods={"generate": {"analytics": ["id"]}})],
        )
        self.assert_rejected(document, "not listed in the caller set")

    def test_unknown_identity_in_an_implementation_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Candidate",
                    implements=["IA"],
                    methods={"generate": method(["billing", "ghost"], ["id"])},
                )
            ],
        )
        self.assert_rejected(document, "ghost")

    def test_returns_for_an_unaccepted_identity_is_refused(self) -> None:
        document = spec_document(
            ["billing", "support"],
            [interface("IA", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Candidate",
                    implements=["IA"],
                    methods={"generate": method(["billing"], {"support": ["id"]})},
                )
            ],
        )
        self.assert_rejected(document, "is not accepted by this implementation")

    def test_extends_a_missing_interface_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IB", extends=["IMissing"])],
        )
        self.assert_rejected(document, "extends unknown interface")

    def test_implements_a_missing_interface_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA")],
            [implementation("Candidate", implements=["IMissing"], methods={})],
        )
        self.assert_rejected(document, "implements unknown interface")

    def test_inheritance_cycle_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [
                interface("IA", extends=["IB"]),
                interface("IB", extends=["IA"]),
            ],
        )
        self.assert_rejected(document, "inheritance cycle")

    def test_duplicate_interface_name_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA"), interface("IA")],
        )
        self.assert_rejected(document, "duplicate interface")

    def test_duplicate_implementation_name_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA")],
            [
                implementation("Candidate", implements=["IA"], methods={}),
                implementation("Candidate", implements=["IA"], methods={}),
            ],
        )
        self.assert_rejected(document, "duplicate implementation")

    def test_method_both_declared_and_exposed_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [
                interface(
                    "IA",
                    methods={"generate": {"billing": ["id"]}},
                    exposes=["generate"],
                )
            ],
        )
        self.assert_rejected(document, "both declared and listed in exposes")

    def test_implementation_without_targets_is_refused(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA")],
            [{"name": "Candidate", "implements": [], "methods": {}}],
        )
        self.assert_rejected(document, "at least one interface")

    def test_accepts_is_required_on_each_implementation_method(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Candidate",
                    implements=["IA"],
                    methods={"generate": {"returns": ["id"]}},
                )
            ],
        )
        self.assert_rejected(document, "accepts: required")

    def test_returns_must_be_a_list_or_mapping(self) -> None:
        document = spec_document(
            ["billing"],
            [interface("IA", methods={"generate": {"billing": ["id"]}})],
            [
                implementation(
                    "Candidate",
                    implements=["IA"],
                    methods={"generate": {"accepts": ["billing"], "returns": "id"}},
                )
            ],
        )
        self.assert_rejected(document, "expected a list of fields or a mapping")

    def test_public_flag_must_be_boolean(self) -> None:
        document = spec_document(
            ["billing"],
            [{"name": "IA", "public": "yes", "methods": {}}],
        )
        self.assert_rejected(document, "expected true or false")

    def test_self_cycle_is_refused(self) -> None:
        document = spec_document(["billing"], [interface("IA", extends=["IA"])])
        self.assert_rejected(document, "inheritance cycle")


if __name__ == "__main__":
    unittest.main()
