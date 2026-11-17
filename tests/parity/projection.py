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

"""Independent projection of rendered JSON / XML documents into one
format-neutral business structure.

The two parsers below are deliberately separate: each understands only its own
wire format (JSON property names such as ``firstIssued`` / ``response`` versus
XML elements such as ``firstIssued`` wrapped in ``responses``). Neither parser
reads the other's output, so the resulting neutral views are independent
evidence rather than one format being asserted through the other.

Timestamps are parsed with their declared timezone and converted to UTC inside
the projection, so comparison is on the same instant rather than on literal
offset spelling.
"""

from datetime import datetime, timezone
from json import loads as json_loads
from typing import Any, Optional
from xml.etree import ElementTree as ET


def normalize_timestamp(value: Optional[str]) -> Optional[str]:
    """Parse an ISO-8601/XSD timestamp and render the same instant in UTC.

    Naive timestamps (no offset) are rejected by the projection callers: the
    fixtures always set an explicit timezone so that JSON and XML can be
    compared on an unambiguous instant. Returns ``None`` for an absent field.
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f'timestamp without timezone information: {value!r}')
    return parsed.astimezone(timezone.utc).isoformat()


def _neutral_analysis(raw: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    return {
        'state': raw.get('state'),
        'justification': raw.get('justification'),
        # JSON wire name is the singular "response"; set semantics are applied
        # at comparison time, raw order is kept here
        'responses': tuple(raw.get('response', ())),
        'detail': raw.get('detail'),
        'first_issued_utc': normalize_timestamp(raw.get('firstIssued')),
        'last_updated_utc': normalize_timestamp(raw.get('lastUpdated')),
    }


def _neutral_affects(raw: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    targets = []
    for target in raw:
        versions = []
        for entry in target.get('versions', ()):
            versions.append({
                # exactly one of version/range is set per model constraint
                'version': entry.get('version'),
                'range': entry.get('range'),
                'status': entry.get('status'),
            })
        targets.append({'ref': target['ref'], 'versions': tuple(versions)})
    return tuple(targets)


def project_json(document: str, vulnerability_ref: str) -> dict[str, Any]:
    """Project a rendered CycloneDX JSON document to the neutral view."""
    doc = json_loads(document)
    vulns = doc.get('vulnerabilities', ())
    matches = [v for v in vulns if v.get('bom-ref') == vulnerability_ref]
    if len(matches) != 1:
        raise ValueError(
            f'expected exactly one JSON vulnerability with bom-ref '
            f'{vulnerability_ref!r}, found {len(matches)}')
    vuln = matches[0]
    return {
        'source_format': 'json',
        'bom_ref': vuln.get('bom-ref'),
        'analysis': _neutral_analysis(vuln.get('analysis')),
        'affects': _neutral_affects(vuln.get('affects', ())),
    }


def _localname(tag: str) -> str:
    return tag.split('}', 1)[-1]


def _child_text(parent: ET.Element, name: str) -> Optional[str]:
    for child in parent:
        if _localname(child.tag) == name:
            return child.text
    return None


def project_xml(document: str, vulnerability_ref: str) -> dict[str, Any]:
    """Project a rendered CycloneDX XML document to the neutral view.

    Namespace agnostic: only local element names are used, so the projection
    does not hard-code the 1.5 versus 1.6 target namespace.
    """
    root = ET.fromstring(document)
    vuln = None
    for candidate in root.iter():
        if _localname(candidate.tag) != 'vulnerability':
            continue
        if candidate.get('bom-ref') == vulnerability_ref:
            vuln = candidate
            break
    if vuln is None:
        raise ValueError(
            f'no XML vulnerability with bom-ref {vulnerability_ref!r}')

    analysis_raw = None
    for child in vuln:
        if _localname(child.tag) == 'analysis':
            analysis_raw = child
            break

    analysis = None
    if analysis_raw is not None:
        responses: list[str] = []
        for ac in analysis_raw:
            name = _localname(ac.tag)
            if name == 'responses':
                responses = [
                    rc.text for rc in ac if _localname(rc.tag) == 'response'
                ]
        analysis = {
            'state': _child_text(analysis_raw, 'state'),
            'justification': _child_text(analysis_raw, 'justification'),
            'responses': tuple(responses),
            'detail': _child_text(analysis_raw, 'detail'),
            'first_issued_utc': normalize_timestamp(
                _child_text(analysis_raw, 'firstIssued')),
            'last_updated_utc': normalize_timestamp(
                _child_text(analysis_raw, 'lastUpdated')),
        }

    affects: list[dict[str, Any]] = []
    for child in vuln:
        if _localname(child.tag) != 'affects':
            continue
        for target in child:
            if _localname(target.tag) != 'target':
                continue
            ref = _child_text(target, 'ref')
            versions: list[dict[str, Any]] = []
            for versions_el in target:
                if _localname(versions_el.tag) != 'versions':
                    continue
                for version_el in versions_el:
                    if _localname(version_el.tag) != 'version':
                        continue
                    versions.append({
                        'version': _child_text(version_el, 'version'),
                        'range': _child_text(version_el, 'range'),
                        'status': _child_text(version_el, 'status'),
                    })
            affects.append({'ref': ref, 'versions': tuple(versions)})

    return {
        'source_format': 'xml',
        'bom_ref': vuln.get('bom-ref'),
        'analysis': analysis,
        'affects': tuple(affects),
    }


def project_business_view(document: str, output_format: str,
                          vulnerability_ref: str) -> dict[str, Any]:
    """Dispatch to the format-specific, independent projector."""
    if output_format == 'json':
        return project_json(document, vulnerability_ref)
    if output_format == 'xml':
        return project_xml(document, vulnerability_ref)
    raise ValueError(f'unknown output_format {output_format!r}')
