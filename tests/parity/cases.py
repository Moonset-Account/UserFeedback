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

"""The four replayable parity cases (C01-C04).

* C01 - full explicit sample: JSON and XML are each projected independently
  for schema 1.5 and 1.6, then compared on state, justification, response set
  and component/version attribution;
* C02 - timezone normalization of timestamps, response set semantics and
  version-range boundary handling, across defaulted and explicit fields;
* C03 - interleaved exports, reordered insertion and duplicate equivalent
  entries keep handling semantics stable and leave the input model unchanged;
* C04 - the failure path: field-level diffs, minimal model and both output
  documents are retained, and no single format is treated as the truth.
"""

import json
import os
from copy import deepcopy
from typing import Any, Callable

from cyclonedx.model.impact_analysis import ImpactAnalysisResponse
from cyclonedx.schema import SchemaVersion

from .checker import Checker
from .diffing import F_RESPONSES, F_STATE, diff_projections
from .engine import (
    SUPPORTED_VERSIONS,
    CaseResult,
    assert_default_omission,
    assert_explicit_expectation,
    cross_check_views,
    render_both,
    write_evidence,
)
from .evidence import EvidenceBundle, RenderBundle, minimal_model_record
from .fixtures import (
    COMPONENT_ALPHA,
    COMPONENT_BETA,
    EXPLICIT_EXPECTATION,
    MINIMAL_VULN_REF,
    VULN_REF,
    build_default_bom,
    build_explicit_bom,
    build_partial_bom,
    snapshot_handling_semantics,
)
from .projection import project_json, project_xml

CASES: dict[str, Callable[[str], CaseResult]] = {}


def _register(case_id: str) -> Callable[[Callable[[str], CaseResult]],
                                        Callable[[str], CaseResult]]:
    def decorator(fn: Callable[[str], CaseResult]
                  ) -> Callable[[str], CaseResult]:
        CASES[case_id] = fn
        fn.case_id = case_id  # type: ignore[attr-defined]
        return fn
    return decorator


def _project_pair(render: RenderBundle, vuln_ref: str
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        project_json(render.json_document, vuln_ref),
        project_xml(render.xml_document, vuln_ref),
    )


@_register('C01')
def case_c01_explicit_parity(evidence_root: str) -> CaseResult:
    """Full explicit handling fields: independent projection, both formats,
    both supported schema versions, no format used as the other's truth."""
    checker = Checker('C01')
    result = CaseResult(case_id='C01', checker=checker,
                        samples=['fixtures.build_explicit_bom'])
    bom = build_explicit_bom()

    for schema_version in SUPPORTED_VERSIONS:
        version = schema_version.to_version()
        render = render_both(bom, schema_version)
        result.renders.append(render)
        json_view, xml_view = _project_pair(render, VULN_REF)

        # each format is checked against the semantic contract on its own
        assert_explicit_expectation(
            checker, json_view, EXPLICIT_EXPECTATION, version)
        assert_explicit_expectation(
            checker, xml_view, EXPLICIT_EXPECTATION, version)
        # only then are the two independent views compared to each other
        result.diffs[version] = cross_check_views(
            checker, json_view, xml_view, version)

        # structural sanity specific to each wire format
        analysis_block = json.loads(render.json_document)[
            'vulnerabilities'][0]['analysis']
        checker.check(
            'response' in analysis_block and 'responses' not in analysis_block,
            f'json/{version}: JSON wire name is "response" (no "responses")',
            field=F_RESPONSES)
        checker.check(
            '<responses>' in render.xml_document and
            '<response>' in render.xml_document,
            f'xml/{version}: XML wraps response entries in <responses>',
            field=F_RESPONSES)

    if not result.ok:
        result.evidence_dir = write_evidence(
            'C01', bom, VULN_REF, result, evidence_root)
    return result


@_register('C02')
def case_c02_timestamps_sets_boundaries(evidence_root: str) -> CaseResult:
    """Timestamps (UTC-normalized), response set semantics and version-range
    boundaries, with explicit and defaulted field samples side by side."""
    checker = Checker('C02')
    result = CaseResult(case_id='C02', checker=checker,
                        samples=['fixtures.build_explicit_bom',
                                 'fixtures.build_default_bom',
                                 'fixtures.build_partial_bom'])

    # --- explicit sample: timezones differ on the wire, instants must match
    explicit_bom = build_explicit_bom()
    for schema_version in SUPPORTED_VERSIONS:
        version = schema_version.to_version()
        render = render_both(explicit_bom, schema_version)
        result.renders.append(render)
        json_view, xml_view = _project_pair(render, VULN_REF)
        result.diffs[version] = cross_check_views(
            checker, json_view, xml_view, version)

        ja, xa = json_view['analysis'], xml_view['analysis']
        # raw offsets differ between the two instants; UTC forms must equal
        checker.check_equal(
            EXPLICIT_EXPECTATION['first_issued_utc'],
            ja['first_issued_utc'],
            f'json/{version}: first_issued normalized to the same UTC instant',
            field='Vulnerability.analysis.first_issued')
        checker.check_equal(
            ja['first_issued_utc'], xa['first_issued_utc'],
            f'{version}: first_issued UTC instant equal across formats',
            field='Vulnerability.analysis.first_issued')
        checker.check_equal(
            ja['last_updated_utc'], xa['last_updated_utc'],
            f'{version}: last_updated UTC instant equal across formats',
            field='Vulnerability.analysis.last_updated')
        # the literal non-UTC offset must actually be present in both formats,
        # proving normalization happened in the projector, not the model
        checker.check(
            '+05:30' in render.json_document and '+05:30' in render.xml_document,
            f'{version}: +05:30 offset retained in both raw documents',
            field='Vulnerability.analysis.first_issued')
        checker.check(
            '-03:00' in render.json_document and '-03:00' in render.xml_document,
            f'{version}: -03:00 offset retained in both raw documents',
            field='Vulnerability.analysis.last_updated')

        # response set semantics: order-insensitive equality already, here
        # assert the exact expected enum values are the complete set
        checker.check_equal(
            {ImpactAnalysisResponse.UPDATE.value,
             ImpactAnalysisResponse.WORKAROUND_AVAILABLE.value},
            set(ja['responses']),
            f'json/{version}: complete response set', field=F_RESPONSES)
        checker.check_equal(
            set(ja['responses']), set(xa['responses']),
            f'{version}: response sets equal across formats',
            field=F_RESPONSES)

        # boundary information: open upper/lower bounds survive verbatim
        alpha_json = next(
            t for t in json_view['affects'] if t['ref'] == COMPONENT_ALPHA)
        alpha_xml = next(
            t for t in xml_view['affects'] if t['ref'] == COMPONENT_ALPHA)
        json_ranges = {v['range'] for v in alpha_json['versions']
                       if v['range'] is not None}
        xml_ranges = {v['range'] for v in alpha_xml['versions']
                      if v['range'] is not None}
        expected_ranges = {'vers:py/>=2.0.0|<3.0.0', 'vers:py/>=4.0.0'}
        checker.check_equal(
            expected_ranges, json_ranges,
            f'json/{version}: range boundaries incl. open bound preserved',
            field='BomTarget.versions[].range')
        checker.check_equal(
            json_ranges, xml_ranges,
            f'{version}: range boundaries equal across formats',
            field='BomTarget.versions[].range')
        beta_json = next(
            t for t in json_view['affects'] if t['ref'] == COMPONENT_BETA)
        open_lower = next(v for v in beta_json['versions']
                          if v['range'] == 'vers:py/<9.0.0')
        checker.check_equal(
            'affected', open_lower['status'],
            f'json/{version}: open-lower-bound range keeps affected status',
            field='BomTarget.versions[].status')

    # --- default sample: omitted fields in both formats, both versions
    default_bom = build_default_bom()
    for schema_version in SUPPORTED_VERSIONS:
        version = schema_version.to_version()
        render = render_both(default_bom, schema_version)
        result.renders.append(render)
        json_view, xml_view = _project_pair(render, MINIMAL_VULN_REF)
        assert_default_omission(checker, json_view, version)
        assert_default_omission(checker, xml_view, version)
        result.diffs.setdefault(version, diff_projections(json_view, xml_view))
        checker.check(
            not result.diffs[version],
            f'{version}: defaulted sample has no JSON/XML divergence',
            field='Vulnerability.analysis')

    # --- partial sample: explicit values beside defaulted ones
    partial_bom = build_partial_bom()
    partial_ref = 'parity-vuln-partial'
    for schema_version in SUPPORTED_VERSIONS:
        version = schema_version.to_version()
        render = render_both(partial_bom, schema_version)
        result.renders.append(render)
        json_view, xml_view = _project_pair(render, partial_ref)
        ja, xa = json_view['analysis'], xml_view['analysis']
        checker.check_equal(
            'in_triage', ja['state'],
            f'json/{version}: explicit state in partial analysis',
            field=F_STATE)
        checker.check_equal(
            ja['state'], xa['state'],
            f'{version}: partial state equal across formats', field=F_STATE)
        checker.check(
            ja['justification'] is None and xa['justification'] is None,
            f'{version}: unset justification stays omitted/None both formats',
            field='Vulnerability.analysis.justification')
        checker.check(
            ja['first_issued_utc'] is None and xa['first_issued_utc'] is None,
            f'{version}: unset first_issued stays None both formats',
            field='Vulnerability.analysis.first_issued')
        checker.check_equal(
            ja['last_updated_utc'], xa['last_updated_utc'],
            f'{version}: explicit last_updated equal across formats',
            field='Vulnerability.analysis.last_updated')
        # defaulted status on a version entry remains None in both formats
        jv = json_view['affects'][0]['versions'][0]
        xv = xml_view['affects'][0]['versions'][0]
        checker.check(
            jv['status'] is None and xv['status'] is None,
            f'{version}: defaulted version status stays None both formats',
            field='BomTarget.versions[].status')
        partial_diff = diff_projections(json_view, xml_view)
        checker.check(
            not partial_diff,
            f'{version}: partial sample has no JSON/XML divergence',
            field='Vulnerability.analysis')

    if not result.ok:
        result.evidence_dir = write_evidence(
            'C02', explicit_bom, VULN_REF, result, evidence_root)
    return result


@_register('C03')
def case_c03_stability_and_model_unchanged(evidence_root: str) -> CaseResult:
    """Interleaved exports, reordered insertion and duplicate equivalent
    entries: handling semantics stay stable and the input model keeps its
    original values after rendering."""
    checker = Checker('C03')
    result = CaseResult(case_id='C03', checker=checker,
                        samples=['fixtures.build_explicit_bom(reverse=True)',
                                 'fixtures.build_explicit_bom'
                                 '(duplicate_equivalent=True)'])

    canonical = build_explicit_bom()
    reversed_bom = build_explicit_bom(reverse=True)
    duplicated_bom = build_explicit_bom(duplicate_equivalent=True)

    before_canonical = snapshot_handling_semantics(canonical, VULN_REF)
    before_reversed = snapshot_handling_semantics(reversed_bom, VULN_REF)
    before_duplicated = snapshot_handling_semantics(duplicated_bom, VULN_REF)

    # model-level equivalence before anything is rendered: insertion order and
    # duplicate equivalent entries cannot change the model's handling values
    checker.check_equal(
        before_canonical, before_reversed,
        'reordered input model keeps identical handling semantics',
        field='Vulnerability.analysis/affects')
    checker.check_equal(
        before_canonical, before_duplicated,
        'duplicate equivalent entries keep identical handling semantics',
        field='Vulnerability.analysis/affects')
    # the duplicate really was deduped inside the SortedSet-backed collections
    dup_analysis = next(
        v for v in duplicated_bom.vulnerabilities if v.bom_ref.value == VULN_REF
    ).analysis
    checker.check_equal(
        2, len(dup_analysis.responses),
        'duplicate response entry appears exactly once in the model',
        field=F_RESPONSES)
    dup_alpha = next(
        t for t in next(v for v in duplicated_bom.vulnerabilities
                        if v.bom_ref.value == VULN_REF).affects
        if t.ref == COMPONENT_ALPHA)
    checker.check_equal(
        3, len(dup_alpha.versions),
        'duplicate equivalent version entry appears exactly once',
        field='Vulnerability.affects[].versions')

    # interleaved exports: alternate formats and schema versions so any
    # hidden cross-call state would surface
    sequence = [
        (SchemaVersion.V1_5, 'json'), (SchemaVersion.V1_5, 'xml'),
        (SchemaVersion.V1_6, 'xml'), (SchemaVersion.V1_6, 'json'),
        (SchemaVersion.V1_5, 'json'), (SchemaVersion.V1_6, 'xml'),
    ]
    reference_views: dict[str, dict] = {}
    for bom_label, bom in (('canonical', canonical),
                           ('reordered', reversed_bom),
                           ('duplicated', duplicated_bom)):
        for schema_version, fmt in sequence:
            render = render_both(bom, schema_version)
            result.renders.append(render)
            view = (project_json(render.json_document, VULN_REF)
                    if fmt == 'json'
                    else project_xml(render.xml_document, VULN_REF))
            key = f'{fmt}:{schema_version.to_version()}'
            if key in reference_views:
                diff = diff_projections(reference_views[key], view)
                checker.check(
                    not diff,
                    f'{bom_label} {key}: interleaved export drift '
                    f'({[e.field for e in diff.entries]})',
                    field='Vulnerability.analysis/affects')
            else:
                reference_views[key] = view
            # every rendering, regardless of order, still meets the contract
            assert_explicit_expectation(
                checker, view, EXPLICIT_EXPECTATION,
                schema_version.to_version())

    # render one Bom twice and confirm the output is byte-stable as well
    render_a = render_both(canonical, SchemaVersion.V1_6)
    render_b = render_both(canonical, SchemaVersion.V1_6)
    result.renders.extend((render_a, render_b))
    checker.check_equal(
        render_a.json_document, render_b.json_document,
        'repeated JSON export of the same model is deterministic',
        field='Vulnerability')
    checker.check_equal(
        render_a.xml_document, render_b.xml_document,
        'repeated XML export of the same model is deterministic',
        field='Vulnerability')

    # input model must still hold its original values after all exports
    after_canonical = snapshot_handling_semantics(canonical, VULN_REF)
    after_reversed = snapshot_handling_semantics(reversed_bom, VULN_REF)
    after_duplicated = snapshot_handling_semantics(duplicated_bom, VULN_REF)
    checker.check_equal(
        before_canonical, after_canonical,
        'canonical input model unchanged after interleaved exports',
        field='Vulnerability.analysis/affects')
    checker.check_equal(
        before_reversed, after_reversed,
        'reordered input model unchanged after interleaved exports',
        field='Vulnerability.analysis/affects')
    checker.check_equal(
        before_duplicated, after_duplicated,
        'duplicate-entry input model unchanged after interleaved exports',
        field='Vulnerability.analysis/affects')
    # bom-refs must have been restored by BomRefDiscriminator after export
    refs = {v.bom_ref.value for v in canonical.vulnerabilities}
    checker.check_equal(
        {VULN_REF}, refs,
        'vulnerability bom-ref restored after export',
        field='Vulnerability.bom_ref')

    if not result.ok:
        result.evidence_dir = write_evidence(
            'C03', canonical, VULN_REF, result, evidence_root)
    return result


@_register('C04')
def case_c04_failure_evidence(evidence_root: str) -> CaseResult:
    """Failure-path guarantees: a real divergence produces field-level diffs,
    a minimal model record and both rendered documents in the evidence bundle,
    with neither format promoted to reference truth."""
    checker = Checker('C04')
    result = CaseResult(case_id='C04', checker=checker,
                        samples=['synthetic-mutation of one projected view'])
    bom = build_explicit_bom()
    schema_version = SchemaVersion.V1_6
    render = render_both(bom, schema_version)
    result.renders.append(render)
    json_view, xml_view = _project_pair(render, VULN_REF)

    # sanity: the real renderings themselves are in parity (otherwise the
    # deliberately mutated views below would not be a meaningful drill)
    healthy_diff = diff_projections(json_view, xml_view)
    checker.check(
        not healthy_diff,
        'baseline renderings are semantically equal before mutation drill',
        field='Vulnerability.analysis')

    # simulate a JSON-side state divergence and an XML-side response loss:
    # mutations live only in the neutral views, production code is untouched
    mutated_json = deepcopy(json_view)
    mutated_xml = deepcopy(xml_view)
    mutated_json['analysis']['state'] = 'exploitable'
    mutated_xml['analysis']['responses'] = tuple(
        r for r in mutated_xml['analysis']['responses'] if r != 'update')
    # drop one component attribution on the XML side as well
    mutated_xml['affects'] = tuple(
        t for t in mutated_xml['affects'] if t['ref'] != COMPONENT_BETA)

    diff = diff_projections(mutated_json, mutated_xml)
    result.diffs[schema_version.to_version()] = diff
    diff_fields = set(diff.fields)
    checker.check(
        'Vulnerability.analysis.state' in diff_fields,
        'state divergence is reported on Vulnerability.analysis.state',
        field='Vulnerability.analysis.state',
        expected={'Vulnerability.analysis.state'}, observed=diff_fields)
    checker.check(
        'Vulnerability.analysis.responses' in diff_fields,
        'response loss is reported on Vulnerability.analysis.responses',
        field='Vulnerability.analysis.responses')
    checker.check(
        any(f.startswith('Vulnerability.affects') for f in diff_fields),
        'lost component attribution is reported on Vulnerability.affects*',
        field='Vulnerability.affects')

    # every diff entry carries both sides, so neither format is the sole truth
    for entry in diff.entries:
        checker.check(
            entry.field and entry.kind in {'missing', 'unexpected', 'value',
                                           'set'},
            f'diff entry {entry.field} has a model field and valid kind',
            field=entry.field)

    # exercise the real evidence writer with the mutated views
    bundle = EvidenceBundle(
        case_id='C04-drill',
        out_dir=os.path.join(evidence_root, 'C04-drill'),
        model_record=minimal_model_record(bom, VULN_REF),
        renders=[render],
    )
    bundle.add_diff(schema_version.to_version(), diff)
    bundle.add_failures(checker.failures)
    written_dir = bundle.write()
    result.evidence_dir = written_dir
    for filename in ('model.min.json', 'rendered-1.6.json',
                     'rendered-1.6.xml', 'diffs.json', 'summary.json'):
        path = os.path.join(written_dir, filename)
        checker.check(os.path.isfile(path) and os.path.getsize(path) > 0,
                      f'evidence file retained: {filename}',
                      field='evidence')
    with open(os.path.join(written_dir, 'diffs.json'), encoding='utf-8') as fh:
        persisted_diffs = json.load(fh)
    checker.check(
        persisted_diffs.get('1.6') and
        all('field' in e and 'expected' in e and 'observed' in e
            for e in persisted_diffs['1.6']),
        'persisted diffs keep field-level expected/observed evidence',
        field='Vulnerability.analysis')
    with open(os.path.join(written_dir, 'model.min.json'),
              encoding='utf-8') as fh:
        model_record = json.load(fh)
    checker.check_equal(
        VULN_REF, model_record['vulnerability_bom_ref'],
        'minimal model record identifies the vulnerable item',
        field='Vulnerability.bom_ref')
    checker.check_equal(
        EXPLICIT_EXPECTATION['state'],
        model_record['analysis']['state'],
        'minimal model record retains analysis state for reproduction',
        field='Vulnerability.analysis.state')
    # both raw formats retained verbatim, not just one canonicalized truth
    with open(os.path.join(written_dir, 'rendered-1.6.json'),
              encoding='utf-8') as fh:
        retained_json = fh.read()
    with open(os.path.join(written_dir, 'rendered-1.6.xml'),
              encoding='utf-8') as fh:
        retained_xml = fh.read()
    checker.check_equal(
        render.json_document, retained_json,
        'retained JSON output equals the actual rendered document',
        field='evidence')
    checker.check_equal(
        render.xml_document, retained_xml,
        'retained XML output equals the actual rendered document',
        field='evidence')

    if not result.ok:
        result.evidence_dir = write_evidence(
            'C04', bom, VULN_REF, result, evidence_root)
    return result
