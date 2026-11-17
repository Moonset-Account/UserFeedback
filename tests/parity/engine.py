# This file is part of CycloneDX Python Library
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) OWASP Foundation. All Rights Reserved.

"""Rendering engine shared by the four parity cases.

The engine only calls the public output API
(``cyclonedx.output.make_outputter`` / the concrete ``Json`` / ``Xml``
outputters) and the independent projectors, then drives a
:class:`~tests.parity.checker.Checker`. Production code is never modified or
mocked: any divergence observed here is real library behaviour.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from cyclonedx.model.bom import Bom
from cyclonedx.output.json import BY_SCHEMA_VERSION as JSON_OUTPUTTERS
from cyclonedx.output.xml import BY_SCHEMA_VERSION as XML_OUTPUTTERS
from cyclonedx.schema import SchemaVersion

from .checker import Checker
from .diffing import (
    F_AFFECTS,
    F_FIRST_ISSUED,
    F_JUSTIFICATION,
    F_LAST_UPDATED,
    F_RANGE_ENTRY,
    F_RANGE_STATUS,
    F_RESPONSES,
    F_STATE,
    F_VERSION_ENTRY,
    SemanticDiff,
    diff_projections,
)
from .evidence import EvidenceBundle, RenderBundle, minimal_model_record

SUPPORTED_VERSIONS = (SchemaVersion.V1_5, SchemaVersion.V1_6)


@dataclass
class CaseResult:
    case_id: str
    checker: Checker
    renders: list[RenderBundle] = field(default_factory=list)
    diffs: dict[str, SemanticDiff] = field(default_factory=dict)
    evidence_dir: Optional[str] = None
    samples: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # zero assertions is also a failure, enforced by the runner, but a
        # checker with no passed assertions can never be ok either
        return self.checker.failed == 0 and self.checker.passed > 0

    def record(self) -> dict[str, Any]:
        return {
            'case': self.case_id,
            'assertions': {
                'total': self.checker.total,
                'passed': self.checker.passed,
                'failed': self.checker.failed,
                'skipped': self.checker.skipped,
            },
            'samples_or_seeds': self.samples,
            'expected_observed': [
                {
                    'message': f.message,
                    'field': f.field,
                    'expected': f.expected,
                    'observed': f.observed,
                }
                for f in self.checker.failures
            ],
            'diff_fields': sorted({
                entry.field
                for diff in self.diffs.values()
                for entry in diff.entries
            }),
            'evidence_dir': self.evidence_dir,
        }


def render_both(bom: Bom, schema_version: SchemaVersion) -> RenderBundle:
    json_doc = JSON_OUTPUTTERS[schema_version](bom).output_as_string(indent=2)
    xml_doc = XML_OUTPUTTERS[schema_version](bom).output_as_string(indent=2)
    return RenderBundle(
        schema_version=schema_version.to_version(),
        json_document=json_doc,
        xml_document=xml_doc,
    )


def assert_explicit_expectation(checker: Checker, view: dict[str, Any],
                                expectation: dict[str, Any],
                                schema_version: str) -> None:
    """Check one neutral projection against the semantic contract."""
    tag = f'{view["source_format"]}/{schema_version}'
    analysis = view.get('analysis')
    checker.check(analysis is not None,
                  f'{tag}: analysis block must be present',
                  field='Vulnerability.analysis')
    if analysis is None:
        return
    checker.check_equal(
        expectation['state'], analysis['state'],
        f'{tag}: analysis state mismatch', field=F_STATE)
    checker.check_equal(
        expectation['justification'], analysis['justification'],
        f'{tag}: analysis justification mismatch', field=F_JUSTIFICATION)
    # response set semantics, independent of insertion order / duplicates
    checker.check_equal(
        expectation['responses'], set(analysis['responses']),
        f'{tag}: response set mismatch', field=F_RESPONSES)
    checker.check_equal(
        expectation['detail'], analysis['detail'],
        f'{tag}: analysis detail mismatch', field='Vulnerability.analysis.detail')
    # timestamps compared on the UTC-normalized instant, not literal offsets
    checker.check_equal(
        expectation['first_issued_utc'], analysis['first_issued_utc'],
        f'{tag}: first_issued instant mismatch after UTC normalization',
        field=F_FIRST_ISSUED)
    checker.check_equal(
        expectation['last_updated_utc'], analysis['last_updated_utc'],
        f'{tag}: last_updated instant mismatch after UTC normalization',
        field=F_LAST_UPDATED)

    targets = {t['ref']: t for t in view['affects']}
    checker.check_equal(
        set(expectation['affects']), set(targets),
        f'{tag}: affects component attribution mismatch', field=F_AFFECTS)
    for ref, expected_versions in expectation['affects'].items():
        if ref not in targets:
            continue
        observed_versions = set()
        for entry in targets[ref]['versions']:
            if entry['version'] is not None:
                observed_versions.add(
                    ('version', entry['version'], entry['status']))
            else:
                observed_versions.add(
                    ('range', entry['range'], entry['status']))
        checker.check_equal(
            expected_versions, observed_versions,
            f'{tag}: version range set mismatch for {ref}',
            field=f'Vulnerability.affects[].versions({ref})')
        # boundary information must survive verbatim, including open bounds
        for kind, boundary, status in sorted(expected_versions):
            field_path = F_VERSION_ENTRY if kind == 'version' else F_RANGE_ENTRY
            matching = [
                e for e in targets[ref]['versions']
                if e[kind] == boundary
            ]
            checker.check(
                len(matching) == 1,
                f'{tag}: boundary {boundary!r} missing or duplicated for {ref}',
                field=field_path, expected=boundary,
                observed=[e[kind] for e in targets[ref]['versions']])
            if matching:
                checker.check_equal(
                    status, matching[0]['status'],
                    f'{tag}: affected status mismatch for boundary {boundary!r}',
                    field=F_RANGE_STATUS)


def assert_default_omission(checker: Checker, view: dict[str, Any],
                            schema_version: str) -> None:
    """Defaulted handling fields must be omitted, identically, both formats."""
    tag = f'{view["source_format"]}/{schema_version}'
    checker.check(
        view.get('analysis') is None,
        f'{tag}: defaulted analysis must be omitted from output',
        field='Vulnerability.analysis',
        expected=None, observed=view.get('analysis'))
    checker.check_equal(
        (), view.get('affects', ()),
        f'{tag}: defaulted affects must be omitted from output',
        field=F_AFFECTS)


def cross_check_views(checker: Checker, json_view: dict[str, Any],
                      xml_view: dict[str, Any],
                      schema_version: str) -> SemanticDiff:
    diff = diff_projections(json_view, xml_view)
    for entry in diff.entries:
        checker.check(
            False,
            f'{schema_version} JSON/XML semantic divergence: {entry.kind} '
            f'at {entry.field}',
            field=entry.field,
            expected=entry.expected, observed=entry.observed)
    return diff


def write_evidence(case_id: str, bom: Bom, vuln_ref: str,
                   result: CaseResult, evidence_root: str) -> str:
    bundle = EvidenceBundle(
        case_id=case_id,
        out_dir=os.path.join(evidence_root, case_id),
        model_record=minimal_model_record(bom, vuln_ref),
        renders=list(result.renders),
    )
    for version, diff in result.diffs.items():
        bundle.add_diff(version, diff)
    bundle.add_failures(result.checker.failures)
    return bundle.write()
