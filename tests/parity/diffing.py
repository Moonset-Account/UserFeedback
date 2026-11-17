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

"""Field-level semantic diffing of neutral vulnerability projections.

A :class:`SemanticDiff` always names the *model field* on the
``cyclonedx.model.vulnerability`` / ``cyclonedx.model.impact_analysis``
classes that a divergence belongs to, e.g.
``Vulnerability.analysis.responses`` or ``Vulnerability.affects[ref].versions``.
Collections with set semantics (responses, affects targets, version ranges)
are compared as sets, so ordering differences are not reported; scalar and
ordered boundary information is compared value by value.
"""

from dataclasses import dataclass
from typing import Any, Optional

# model field paths used in every reported diff, so evidence points back to
# the actual attribute on cyclonedx.model.vulnerability / impact_analysis
F_STATE = 'Vulnerability.analysis.state'
F_JUSTIFICATION = 'Vulnerability.analysis.justification'
F_RESPONSES = 'Vulnerability.analysis.responses'
F_DETAIL = 'Vulnerability.analysis.detail'
F_FIRST_ISSUED = 'Vulnerability.analysis.first_issued'
F_LAST_UPDATED = 'Vulnerability.analysis.last_updated'
F_AFFECTS = 'Vulnerability.affects'
F_AFFECTS_VERSIONS = 'Vulnerability.affects[].versions'
F_VERSION_ENTRY = 'BomTarget.versions[].version'
F_RANGE_ENTRY = 'BomTarget.versions[].range'
F_RANGE_STATUS = 'BomTarget.versions[].status'


@dataclass
class DiffEntry:
    field: str
    kind: str  # 'missing' | 'unexpected' | 'value' | 'set'
    expected: Any = None
    observed: Any = None
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            'field': self.field,
            'kind': self.kind,
            'expected': self.expected,
            'observed': self.observed,
            'detail': self.detail,
        }


@dataclass
class SemanticDiff:
    entries: list[DiffEntry]

    @property
    def fields(self) -> list[str]:
        # de-duplicated, order-preserving list of model fields with a divergence
        seen: dict[str, None] = {}
        for e in self.entries:
            seen.setdefault(e.field, None)
        return list(seen.keys())

    def __bool__(self) -> bool:
        return bool(self.entries)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.entries]


def _diff_scalar(entries: list[DiffEntry], field: str,
                 expected: Any, observed: Any) -> None:
    if expected != observed:
        entries.append(DiffEntry(
            field=field, kind='value',
            expected=expected, observed=observed,
        ))


def _diff_set(entries: list[DiffEntry], field: str,
              expected: set[Any], observed: set[Any]) -> None:
    missing = expected - observed
    unexpected = observed - expected
    if missing:
        entries.append(DiffEntry(
            field=field, kind='missing',
            expected=sorted(missing, key=lambda v: str(v)), observed=None,
            detail='present in JSON projection, absent from XML projection',
        ))
    if unexpected:
        entries.append(DiffEntry(
            field=field, kind='unexpected',
            expected=None, observed=sorted(unexpected, key=lambda v: str(v)),
            detail='present in XML projection, absent from JSON projection',
        ))


def diff_projections(json_view: dict[str, Any],
                     xml_view: dict[str, Any]) -> SemanticDiff:
    """Compare two independently projected neutral structures.

    ``json_view`` / ``xml_view`` are the outputs of
    :func:`tests.parity.projection.project_business_view` and only serve as
    the two sides of the comparison - neither one is the reference truth.
    Timestamps must already be normalized to UTC; responses and version
    ranges are compared with set semantics while range boundary strings and
    their affected statuses keep per-entry identity.
    """
    entries: list[DiffEntry] = []
    ja = json_view.get('analysis') or {}
    xa = xml_view.get('analysis') or {}

    _diff_scalar(entries, F_STATE, ja.get('state'), xa.get('state'))
    _diff_scalar(entries, F_JUSTIFICATION,
                 ja.get('justification'), xa.get('justification'))
    _diff_scalar(entries, F_DETAIL, ja.get('detail'), xa.get('detail'))
    _diff_scalar(entries, F_FIRST_ISSUED,
                 ja.get('first_issued_utc'), xa.get('first_issued_utc'))
    _diff_scalar(entries, F_LAST_UPDATED,
                 ja.get('last_updated_utc'), xa.get('last_updated_utc'))

    # response set semantics: order and duplicate insertion are irrelevant
    _diff_set(
        entries, F_RESPONSES,
        set(ja.get('responses', ())), set(xa.get('responses', ())),
    )

    j_targets = {t['ref']: t for t in json_view.get('affects', ())}
    x_targets = {t['ref']: t for t in xml_view.get('affects', ())}
    _diff_set(
        entries, F_AFFECTS,
        set(j_targets.keys()), set(x_targets.keys()),
    )
    for ref in sorted(set(j_targets) & set(x_targets)):
        # each entry keeps its exact version/range boundary plus affected
        # status, so a divergence in either lands on a concrete model field
        jv = {(v.get('version'), v.get('range'), v.get('status'))
              for v in j_targets[ref]['versions']}
        xv = {(v.get('version'), v.get('range'), v.get('status'))
              for v in x_targets[ref]['versions']}
        _diff_set(
            entries, f'{F_AFFECTS_VERSIONS}({ref})', jv, xv,
        )

    return SemanticDiff(entries)
