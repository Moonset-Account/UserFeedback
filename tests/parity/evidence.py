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

"""Failure evidence: minimal reproducible model plus both rendered documents.

When a parity assertion fails, the wrapper needs enough material to reproduce
and audit the divergence without re-running anything:

* ``model.min.json`` - the smallest neutral description of the input model
  that still carries the handling fields under test;
* ``rendered-<version>.json`` / ``rendered-<version>.xml`` - the two output
  documents exactly as produced by the library (no normalization, so both
  formats remain first-class evidence instead of one being treated as truth);
* ``diffs.json`` - every field-level
  :class:`~tests.parity.diffing.SemanticDiff` entry, naming the model field;
* ``summary.json`` - case id, schema versions, assertion counts, messages.

Only synthetic fixture data is ever written here.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .diffing import SemanticDiff


def json_default(obj: Any) -> Any:
    """JSON fallback for the plain-value types used in evidence records."""
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=lambda v: str(v))
    if isinstance(obj, datetime):
        return obj.isoformat()
    return repr(obj)


@dataclass
class RenderBundle:
    """Both rendered documents for one schema version, kept verbatim."""

    schema_version: str
    json_document: str
    xml_document: str

    def as_summary(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'json_bytes': len(self.json_document.encode('utf-8')),
            'xml_bytes': len(self.xml_document.encode('utf-8')),
        }


def minimal_model_record(bom: Any, vuln_ref: str) -> dict[str, Any]:
    """Extract only the handling-related model values as a minimal record."""
    vuln = next(v for v in bom.vulnerabilities if v.bom_ref.value == vuln_ref)
    record: dict[str, Any] = {
        'vulnerability_bom_ref': vuln.bom_ref.value,
        'vulnerability_id': vuln.id,
        'analysis': None,
        'affects': [],
    }
    analysis = vuln.analysis
    if analysis is not None:
        record['analysis'] = {
            'state': analysis.state.value if analysis.state else None,
            'justification':
                analysis.justification.value if analysis.justification else None,
            'responses': [r.value for r in analysis.responses],
            'detail': analysis.detail,
            'first_issued':
                analysis.first_issued.isoformat() if analysis.first_issued else None,
            'last_updated':
                analysis.last_updated.isoformat() if analysis.last_updated else None,
        }
    for target in vuln.affects:
        record['affects'].append({
            'ref': target.ref,
            'versions': [
                {
                    'version': entry.version,
                    'range': entry.range,
                    'status': entry.status.value if entry.status else None,
                }
                for entry in target.versions
            ],
        })
    return record


@dataclass
class EvidenceBundle:
    case_id: str
    out_dir: str
    model_record: dict[str, Any]
    renders: list[RenderBundle] = field(default_factory=list)
    diffs_by_version: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failure_messages: list[dict[str, Any]] = field(default_factory=list)
    written: Optional[str] = None

    def add_diff(self, schema_version: str, diff: SemanticDiff) -> None:
        if diff:
            self.diffs_by_version[schema_version] = diff.as_dicts()

    def add_failures(self, failures: list[Any]) -> None:
        for failure in failures:
            self.failure_messages.append({
                'message': failure.message,
                'field': failure.field,
                'expected': failure.expected,
                'observed': failure.observed,
            })

    def write(self) -> str:
        """Persist the bundle and return the directory path."""
        os.makedirs(self.out_dir, exist_ok=True)
        with open(os.path.join(self.out_dir, 'model.min.json'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            json.dump(self.model_record, fh, indent=2, sort_keys=True,
                      default=json_default)
            fh.write('\n')
        for render in self.renders:
            with open(os.path.join(
                    self.out_dir,
                    f'rendered-{render.schema_version}.json'),
                    'w', encoding='utf-8', newline='\n') as fh:
                fh.write(render.json_document)
            with open(os.path.join(
                    self.out_dir,
                    f'rendered-{render.schema_version}.xml'),
                    'w', encoding='utf-8', newline='\n') as fh:
                fh.write(render.xml_document)
        with open(os.path.join(self.out_dir, 'diffs.json'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            json.dump(self.diffs_by_version, fh, indent=2, sort_keys=True,
                      default=json_default)
            fh.write('\n')
        with open(os.path.join(self.out_dir, 'summary.json'), 'w',
                  encoding='utf-8', newline='\n') as fh:
            json.dump({
                'case_id': self.case_id,
                'renders': [r.as_summary() for r in self.renders],
                'diff_fields': sorted({
                    entry['field']
                    for entries in self.diffs_by_version.values()
                    for entry in entries
                }),
                'failures': self.failure_messages,
            }, fh, indent=2, sort_keys=True, default=json_default)
            fh.write('\n')
        self.written = self.out_dir
        return self.out_dir
