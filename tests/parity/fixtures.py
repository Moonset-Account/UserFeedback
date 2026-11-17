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

"""Deterministic, locally synthesized model fixtures.

No external services, credentials or real advisory data are involved: every
value below is synthetic. Two kinds of samples are provided on purpose:

* :func:`build_explicit_bom` fills every handling field (state, justification,
  responses, first/last update timestamps) and several ``affects`` targets with
  both exact versions and ``vers`` ranges including open boundaries;
* :func:`build_default_bom` leaves all handling fields at their model defaults,
  so omission behaviour can be checked separately from explicit values.

Timestamps use explicit, different non-UTC offsets so the tests prove that the
two formats carry the same instant after timezone normalization rather than
merely the same literal string.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from cyclonedx.model.bom import Bom
from cyclonedx.model.component import Component, ComponentType
from cyclonedx.model.impact_analysis import (
    ImpactAnalysisAffectedStatus,
    ImpactAnalysisJustification,
    ImpactAnalysisResponse,
    ImpactAnalysisState,
)
from cyclonedx.model.vulnerability import (
    BomTarget,
    BomTargetVersionRange,
    Vulnerability,
    VulnerabilityAnalysis,
)

VULN_REF = 'parity-vuln-explicit'
MINIMAL_VULN_REF = 'parity-vuln-default'
COMPONENT_ALPHA = 'parity-component-alpha'
COMPONENT_BETA = 'parity-component-beta'

# fixed, replayable instants with different explicit offsets
FIRST_ISSUED = datetime(2026, 1, 2, 3, 4, 5, 678000,
                        tzinfo=timezone(timedelta(hours=5, minutes=30)))
LAST_UPDATED = datetime(2026, 6, 7, 8, 9, 10,
                        tzinfo=timezone(timedelta(hours=-3)))
FIRST_ISSUED_UTC = '2026-01-01T21:34:05.678000+00:00'
LAST_UPDATED_UTC = '2026-06-07T11:09:10+00:00'

# expected neutral business values (the semantic contract, not a wire format)
EXPLICIT_EXPECTATION: dict[str, Any] = {
    'state': ImpactAnalysisState.NOT_AFFECTED.value,
    'justification': ImpactAnalysisJustification.CODE_NOT_PRESENT.value,
    'responses': {
        ImpactAnalysisResponse.UPDATE.value,
        ImpactAnalysisResponse.WORKAROUND_AVAILABLE.value,
    },
    'detail': 'synthetic analysis detail for parity tests',
    'first_issued_utc': FIRST_ISSUED_UTC,
    'last_updated_utc': LAST_UPDATED_UTC,
    'affects': {
        COMPONENT_ALPHA: {
            ('version', '1.0.0', ImpactAnalysisAffectedStatus.UNAFFECTED.value),
            ('range', 'vers:py/>=2.0.0|<3.0.0',
             ImpactAnalysisAffectedStatus.AFFECTED.value),
            ('range', 'vers:py/>=4.0.0',
             ImpactAnalysisAffectedStatus.UNKNOWN.value),
        },
        COMPONENT_BETA: {
            ('version', '7.1.0', ImpactAnalysisAffectedStatus.AFFECTED.value),
            ('range', 'vers:py/<9.0.0',
             ImpactAnalysisAffectedStatus.AFFECTED.value),
        },
    },
}


def _components() -> tuple[Component, Component]:
    return (
        Component(name='lib-alpha', version='2.0.0',
                  type=ComponentType.LIBRARY, bom_ref=COMPONENT_ALPHA),
        Component(name='lib-beta', version='7.1.0',
                  type=ComponentType.LIBRARY, bom_ref=COMPONENT_BETA),
    )


def _analysis(responses: list[ImpactAnalysisResponse] | None = None
              ) -> VulnerabilityAnalysis:
    return VulnerabilityAnalysis(
        state=ImpactAnalysisState.NOT_AFFECTED,
        justification=ImpactAnalysisJustification.CODE_NOT_PRESENT,
        responses=responses if responses is not None else [
            ImpactAnalysisResponse.UPDATE,
            ImpactAnalysisResponse.WORKAROUND_AVAILABLE,
        ],
        detail='synthetic analysis detail for parity tests',
        first_issued=FIRST_ISSUED,
        last_updated=LAST_UPDATED,
    )


def _alpha_versions() -> list[BomTargetVersionRange]:
    # insertion order intentionally mixes exact versions and ranges;
    # one range has an open upper boundary (>=4.0.0 with no upper limit)
    return [
        BomTargetVersionRange(range='vers:py/>=4.0.0',
                              status=ImpactAnalysisAffectedStatus.UNKNOWN),
        BomTargetVersionRange(version='1.0.0',
                              status=ImpactAnalysisAffectedStatus.UNAFFECTED),
        BomTargetVersionRange(range='vers:py/>=2.0.0|<3.0.0',
                              status=ImpactAnalysisAffectedStatus.AFFECTED),
    ]


def _beta_versions() -> list[BomTargetVersionRange]:
    return [
        BomTargetVersionRange(version='7.1.0',
                              status=ImpactAnalysisAffectedStatus.AFFECTED),
        BomTargetVersionRange(range='vers:py/<9.0.0',
                              status=ImpactAnalysisAffectedStatus.AFFECTED),
    ]


def build_explicit_bom(*, responses: list[ImpactAnalysisResponse] | None = None,
                       reverse: bool = False,
                       duplicate_equivalent: bool = False) -> Bom:
    """Build the fully populated sample Bom.

    ``reverse`` swaps insertion order of targets / responses / ranges while
    keeping the same logical content (set semantics must absorb it).
    ``duplicate_equivalent`` adds an extra equal entry inside the
    SortedSet-backed collections (a response enum and an equivalent version
    range), which must neither change semantics nor appear twice.
    """
    alpha_versions = list(_alpha_versions())
    beta_versions = list(_beta_versions())
    if reverse:
        alpha_versions.reverse()
        beta_versions.reverse()
    if duplicate_equivalent:
        # equivalent BomTargetVersionRange appended to the same target:
        # the SortedSet must collapse it to the single original entry
        alpha_versions.append(BomTargetVersionRange(
            range='vers:py/>=2.0.0|<3.0.0',
            status=ImpactAnalysisAffectedStatus.AFFECTED))

    alpha_target = BomTarget(ref=COMPONENT_ALPHA, versions=alpha_versions)
    beta_target = BomTarget(ref=COMPONENT_BETA, versions=beta_versions)
    affects = [beta_target, alpha_target] if reverse else \
        [alpha_target, beta_target]

    response_list = responses
    if response_list is None:
        response_list = [
            ImpactAnalysisResponse.UPDATE,
            ImpactAnalysisResponse.WORKAROUND_AVAILABLE,
        ]
        if reverse:
            response_list.reverse()
    if duplicate_equivalent:
        # equivalent enum instance appended again - SortedSet dedupes it
        response_list = response_list + [ImpactAnalysisResponse.UPDATE]

    bom = Bom()
    for component in reversed(_components()) if reverse else _components():
        bom.components.add(component)
    bom.vulnerabilities.add(Vulnerability(
        bom_ref=VULN_REF,
        id='CVE-2026-9001',
        analysis=_analysis(response_list),
        affects=affects,
    ))
    return bom


def build_default_bom() -> Bom:
    """Sample with every handling field left at model defaults (omitted)."""
    bom = Bom()
    bom.components.add(Component(
        name='lib-default', version='0.1.0',
        type=ComponentType.LIBRARY, bom_ref=COMPONENT_ALPHA,
    ))
    bom.vulnerabilities.add(Vulnerability(
        bom_ref=MINIMAL_VULN_REF,
        id='CVE-2026-9002',
        # analysis omitted entirely; affects omitted entirely
    ))
    return bom


def build_partial_bom() -> Bom:
    """Sample with only some analysis fields set, exercising explicit
    values next to defaulted ones within the same analysis object."""
    bom = Bom()
    bom.components.add(Component(
        name='lib-partial', version='3.3.3',
        type=ComponentType.LIBRARY, bom_ref=COMPONENT_BETA,
    ))
    bom.vulnerabilities.add(Vulnerability(
        bom_ref='parity-vuln-partial',
        id='CVE-2026-9003',
        analysis=VulnerabilityAnalysis(
            state=ImpactAnalysisState.IN_TRIAGE,
            responses=[ImpactAnalysisResponse.UPDATE],
            last_updated=LAST_UPDATED,
            # justification, detail, first_issued stay None / default
        ),
        affects=[
            BomTarget(ref=COMPONENT_BETA, versions=[
                BomTargetVersionRange(version='3.3.3'),  # status defaulted
            ]),
        ],
    ))
    return bom


def snapshot_handling_semantics(bom: Bom, vuln_ref: str) -> dict[str, Any]:
    """Pure-value snapshot of the vulnerability handling fields of a model.

    Used to prove that rendering (which runs ``Bom.validate`` and the
    ``BomRefDiscriminator``) does not change the input model's handling
    values. Only plain values / tuples / frozensets are returned, so the
    snapshot can be compared after the Bom was exported.
    """
    vuln = next(v for v in bom.vulnerabilities if v.bom_ref.value == vuln_ref)
    analysis = vuln.analysis
    analysis_snapshot = None
    if analysis is not None:
        analysis_snapshot = {
            'state': analysis.state.value if analysis.state else None,
            'justification':
                analysis.justification.value if analysis.justification else None,
            'responses': frozenset(r.value for r in analysis.responses),
            'detail': analysis.detail,
            'first_issued': deepcopy(analysis.first_issued),
            'last_updated': deepcopy(analysis.last_updated),
        }
    affects_snapshot = {}
    for target in vuln.affects:
        affects_snapshot[target.ref] = frozenset(
            (entry.version, entry.range,
             entry.status.value if entry.status else None)
            for entry in target.versions
        )
    return {
        'bom_ref': vuln.bom_ref.value,
        'id': vuln.id,
        'analysis': analysis_snapshot,
        'affects': affects_snapshot,
    }
