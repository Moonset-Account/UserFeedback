#!/usr/bin/env python3
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

"""Local verification entry point for the vulnerability-handling parity work.

This wrapper is a *runner*, not an independent proof of correctness: it calls
the real case implementations in ``tests.parity.cases`` (which exercise the
library's actual JSON/XML outputters and assert on real values) and reports
per-case JSON records with the actual assertion counts. Any failed assertion,
a missing/unimplemented case entry, zero assertions or a fully skipped case
causes a non-zero exit code - printing a success message alone is never enough.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case all

Evidence from failed runs (minimal model, both rendered documents, field-level
diffs) is written under tests/_data/parity-evidence/<CASE>/. All fixtures are
synthetic; no credentials or private data are involved.
"""

import argparse
import json
import os
import shutil
import sys

# make the repo root importable when the script is run directly, without
# requiring an editable install of the package
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.parity.cases import CASES  # noqa: E402

_EVIDENCE_ROOT = os.path.join(
    _REPO_ROOT, 'tests', '_data', 'parity-evidence')

CASE_DESCRIPTIONS = {
    'C01': 'Explicit analysis state/justification/responses and affects '
           'attribution projected independently from JSON and XML for '
           'schema 1.5 and 1.6',
    'C02': 'Timestamp UTC normalization, response set semantics, version-range '
           'boundaries; defaulted and explicit field samples',
    'C03': 'Interleaved exports, insertion reorder and duplicate equivalent '
           'entries: stable semantics, input model left unchanged',
    'C04': 'Failure path: minimal model + both output formats + field-level '
           'diffs retained; neither format is the sole truth',
}


def list_cases() -> int:
    print(json.dumps({
        'cases': [
            {'case': case_id, 'description': CASE_DESCRIPTIONS.get(case_id, '')}
            for case_id in sorted(CASES)
        ],
    }, indent=2))
    return 0


def run_case(case_id: str) -> tuple[dict, int]:
    if case_id not in CASES:
        record = {
            'case': case_id,
            'error': 'unknown or unimplemented case',
            'known_cases': sorted(CASES),
        }
        return record, 3

    case_dir = os.path.join(_EVIDENCE_ROOT, case_id)
    shutil.rmtree(case_dir, ignore_errors=True)
    try:
        result = CASES[case_id](case_dir)
    except NotImplementedError as exc:
        return {'case': case_id, 'error': f'not implemented: {exc}'}, 4
    except Exception as exc:  # noqa: BLE001 - runner must report, not swallow
        return {
            'case': case_id,
            'error': f'{type(exc).__name__}: {exc}',
        }, 5

    record = result.record()
    counts = record['assertions']
    exit_code = 0
    if counts['failed']:
        exit_code = 1
    elif counts['passed'] == 0:
        # zero assertions or a fully skipped case prove nothing
        record['error'] = (
            'no effective assertions: '
            f"total={counts['total']} skipped={counts['skipped']}")
        exit_code = 2
    return record, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run vulnerability-handling JSON/XML parity verification.')
    parser.add_argument('--list', action='store_true',
                        help='list available verification cases')
    parser.add_argument('--case', dest='case_id',
                        help='case id to run (C01..C04), or "all"')
    args = parser.parse_args(argv)

    if args.list:
        return list_cases()

    if not args.case_id:
        parser.print_help()
        return 2

    selected = sorted(CASES) if args.case_id == 'all' else [args.case_id]
    overall_exit = 0
    records = []
    for case_id in selected:
        record, exit_code = run_case(case_id)
        records.append(record)
        overall_exit = max(overall_exit, exit_code)

    print(json.dumps(
        {'results': records} if len(records) > 1 else records[0],
        indent=2, default=lambda o: sorted(o, key=lambda v: str(v))
        if isinstance(o, (set, frozenset)) else str(o)))
    return overall_exit


if __name__ == '__main__':
    raise SystemExit(main())
