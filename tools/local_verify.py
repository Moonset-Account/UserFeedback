#!/usr/bin/env python3
"""Local verification entry for the stateful authorization-code exchange tests.

Runs the real assertions implemented in
``tests/oauth2/rfc6749/grant_types/test_authorization_code_stateful.py``
against the in-tree oauthlib sources and prints one JSON record per case.

Usage (from the workspace root, using the workspace interpreter):

    .venv/bin/python tools/local_verify.py --list
    .venv/bin/python tools/local_verify.py --case C01
    .venv/bin/python tools/local_verify.py            # runs all cases

Exit status is non-zero if any case is missing, raises, runs zero
assertions, skips, or has a failed assertion.  Output never contains
secrets or raw sensitive samples; only synthetic fixture identifiers,
stage names and token ids are reported.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.oauth2.rfc6749.grant_types import (  # noqa: E402
    test_authorization_code_stateful as stateful,
)

CASES = {
    'C01': ('Stateful single-client exchange stage model',
            stateful.run_case_c01),
    'C02': ('Interleaved two-client code/verifier bindings',
            stateful.run_case_c02),
    'C03': ('Failure retry and post-success replay',
            stateful.run_case_c03),
    'C04': ('Deterministic tokens and cross-request isolation',
            stateful.run_case_c04),
}


def run_case(name, case_fn):
    """Execute one case and build its JSON record plus exit code."""
    try:
        result = case_fn()
    except Exception as exc:  # verification must record, not crash
        record = {
            'case': name,
            'assertions': 0,
            'passed': 0,
            'failed': 1,
            'skipped': 0,
            'seed': None,
            'summary': {'error': '%s: %s' % (type(exc).__name__, exc)},
        }
        return record, 1

    assertions = result.get('assertions', 0)
    failures = result.get('failures', [])
    skipped = result.get('skipped', 0)
    record = {
        'case': name,
        'assertions': assertions,
        'passed': assertions - len(failures),
        'failed': len(failures),
        'skipped': skipped,
        'seed': result.get('seed'),
        'summary': result.get('summary', {}),
    }
    if failures:
        record['failures'] = failures
    ok = assertions > 0 and not failures and skipped == 0
    return record, 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true',
                        help='list available cases and exit')
    parser.add_argument('--case', default=None,
                        help='case id to run (C01-C04 or ALL; default: ALL)')
    args = parser.parse_args(argv)

    if args.list:
        listing = [{'id': case_id, 'title': title}
                   for case_id, (title, _) in sorted(CASES.items())]
        print(json.dumps({'cases': listing}, indent=2))
        return 0

    if args.case is None or args.case == 'ALL':
        selected = sorted(CASES)
    else:
        selected = [args.case]

    exit_code = 0
    for name in selected:
        entry = CASES.get(name)
        if entry is None:
            print(json.dumps({'case': name,
                              'error': 'missing case entry'}))
            exit_code = 1
            continue
        record, code = run_case(name, entry[1])
        print(json.dumps(record, ensure_ascii=False))
        exit_code = exit_code or code
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
