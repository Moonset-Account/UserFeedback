#!/usr/bin/env python
"""Local verification entry point for the DUO123 intra-procedural analysis.

Runs real assertions against the implemented linter and analysis helper using
synthetic, self-contained samples. No network access, no credentials, and no
real source tree data are involved.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C02
    python tools/local_verify.py --case C03
    python tools/local_verify.py --case C04

Exit status is non-zero if any assertion fails, a case is missing, a case
runs zero assertions, or an entire case is skipped. Each case prints one JSON
record containing the assertion counts and expected/observed summaries.
"""

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import argparse
import ast
import json
import os
import sys
import textwrap

# Make the workspace importable when run as a plain script.
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

import dlint  # noqa: E402
from dlint import namespace  # noqa: E402
from dlint.linters.helpers import intraproc_verify_analysis as iva  # noqa: E402


CONFIRMED_PREFIX = 'DUO123 use of "verify=False"'
REVIEW_PREFIX = 'DUO123 unable to determine "verify" value'


class AssertionBookkeeper(object):
    """Counts assertions and records the first failure, if any."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []

    def check(self, description, condition, expected, observed):
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append({
                'assertion': description,
                'expected': expected,
                'observed': observed,
            })

    def check_equal(self, description, expected, observed):
        self.check(
            description, expected == observed,
            expected=expected, observed=observed
        )


def parse(source):
    return ast.parse(textwrap.dedent(source))


def run_linter(source, intraproc_analysis):
    node = parse(source)
    linter = dlint.linters.BadRequestsUseLinter(
        intraproc_analysis=intraproc_analysis
    )
    linter.visit(node)
    return linter.get_results()


def run_analyzer(source):
    node = parse(source)
    ns = namespace.Namespace.from_module_node(node)
    findings = iva.analyze_module(node, ns)
    return {
        finding.node.lineno: (finding.state, sorted(finding.origins))
        for finding in findings.values()
    }


def classify_results(results):
    return sorted(
        (result.lineno, 'confirmed' if result.message.startswith(
            CONFIRMED_PREFIX) else 'review')
        for result in results
    )


# --------------------------------------------------------------------------
# Case C01: boolean constants, variable copies, ordering overwrite, and the
# requirement that only confirmed requests-bound objects are targets.
# --------------------------------------------------------------------------

C01_SAMPLE = """
    import requests

    requests.get(verify=False)
    requests.get(verify=True)

    def f():
        v = False
        w = v
        requests.get(verify=w)
        v = True
        requests.get(verify=v)

    class Fake(object):
        def get(self, **kwargs):
            return None

    def g():
        requests = Fake()
        requests.get(verify=False)
"""


def case_c01():
    book = AssertionBookkeeper()
    findings = run_analyzer(C01_SAMPLE)
    results = run_linter(C01_SAMPLE, intraproc_analysis=True)
    legacy = run_linter(C01_SAMPLE, intraproc_analysis=False)

    # Boolean constant: direct False is confirmed at its real line, True is
    # a TRUE finding (not a risk), no-verify calls do not appear.
    book.check_equal(
        'direct verify=False classifies FALSE on the call line',
        findings.get(4), (iva.FALSE, [4])
    )
    book.check_equal(
        'direct verify=True classifies TRUE, never a risk result',
        findings.get(5), (iva.TRUE, [])
    )

    # Variable copy preserves the value and the original assignment line.
    # Sample layout: line 8 is ``v = False``, line 9 the copy, line 10 the
    # call that consumes the copied value.
    book.check_equal(
        'copied variable keeps FALSE and origin assignment line',
        findings.get(10), (iva.FALSE, [8])
    )

    # Ordering overwrite: the later binding is the one used at the call.
    book.check_equal(
        'later reassignment overwrites the earlier FALSE value',
        findings.get(12), (iva.TRUE, [])
    )

    # Same-named custom object is not treated as the requests library by
    # the intra-procedural analysis (line 20 is the shadowed-object call).
    book.check(
        'locally rebound requests name produces no finding',
        20 not in findings,
        expected='no finding for line 20',
        observed=sorted(findings)
    )

    # Linter-level classification matches the analyzer states.
    book.check_equal(
        'linter emits exactly the two confirmed DUO123 results',
        classify_results(results),
        [(4, 'confirmed'), (10, 'confirmed')]
    )
    book.check_equal(
        'confirmed message carries the origin assignment line',
        results[1].message,
        'DUO123 use of "verify=False" is insecure in "requests" module '
        '(value assigned at line 8)'
    )

    # Default mode keeps the historical behavior: only the direct keyword
    # call is detected. The legacy visitor intentionally has no local-scope
    # awareness, so it cannot distinguish a same-named custom object
    # (line 20) either; that accepted boundary is covered by the new mode
    # above and documented in the README.
    book.check_equal(
        'legacy mode result set is unchanged by the new analysis',
        classify_results(legacy), [(4, 'confirmed'), (20, 'confirmed')]
    )

    return book, {
        'sample_lines': 20,
        'analyzer_findings': findings,
        'linter_results': classify_results(results),
        'legacy_results': classify_results(legacy),
        'legacy_boundary': (
            'legacy mode is import/namespace based and cannot see local '
            'rebinding; intraproc mode correctly excludes the line-20 call'
        ),
    }


# --------------------------------------------------------------------------
# Case C02: static/unknown conditions, true/false merges, early return and
# unreachable calls, plus confirmed/review/no-risk classification.
# --------------------------------------------------------------------------

C02_STATIC_SAMPLE = """
    import requests

    def f():
        if True:
            requests.get(verify=False)
        else:
            requests.get(verify=True)
        if False:
            requests.get(verify=False)
        else:
            requests.get(verify=True)
"""

C02_MERGE_SAMPLE = """
    import requests

    def f(flag):
        if flag:
            v = False
        else:
            v = True
        requests.get(verify=v)
"""

C02_RETURN_SAMPLE = """
    import requests

    def f(flag):
        if flag:
            return
        requests.get(verify=False)
"""

C02_UNREACHABLE_SAMPLE = """
    import requests

    def f():
        return
        requests.get(verify=False)
"""


def case_c02():
    book = AssertionBookkeeper()

    static_findings = run_analyzer(C02_STATIC_SAMPLE)
    book.check_equal(
        'statically true condition takes only the true branch',
        static_findings.get(6), (iva.FALSE, [6])
    )
    book.check(
        'dead else-branch call is never recorded',
        8 not in static_findings,
        expected='no line 8 finding',
        observed=sorted(static_findings)
    )
    book.check_equal(
        'statically false condition takes only the else branch',
        static_findings.get(12), (iva.TRUE, [])
    )
    book.check(
        'dead if-branch call is never recorded',
        10 not in static_findings,
        expected='no line 10 finding',
        observed=sorted(static_findings)
    )

    merge_findings = run_analyzer(C02_MERGE_SAMPLE)
    merge_results = run_linter(C02_MERGE_SAMPLE, intraproc_analysis=True)
    book.check_equal(
        'true/false path merge is TOP, never confirmed-safe',
        merge_findings.get(9), (iva.TOP, [])
    )
    book.check_equal(
        'TOP state surfaces as the independent review message',
        classify_results(merge_results), [(9, 'review')]
    )
    book.check(
        'review message is distinct from the confirmed DUO123 message',
        merge_results[0].message != dlint.linters.BadRequestsUseLinter._error_tmpl,
        expected='different message from the confirmed template',
        observed=merge_results[0].message
    )

    return_findings = run_analyzer(C02_RETURN_SAMPLE)
    book.check_equal(
        'early return isolates the single reachable insecure path',
        return_findings.get(7), (iva.FALSE, [7])
    )

    unreachable_findings = run_analyzer(C02_UNREACHABLE_SAMPLE)
    unreachable_results = run_linter(
        C02_UNREACHABLE_SAMPLE, intraproc_analysis=True
    )
    book.check_equal(
        'unreachable calls produce no analyzer findings',
        unreachable_findings, {}
    )
    book.check_equal(
        'unreachable calls produce no linter results',
        classify_results(unreachable_results), []
    )

    return book, {
        'static_findings': static_findings,
        'merge_findings': merge_findings,
        'merge_results': classify_results(merge_results),
        'early_return_findings': return_findings,
        'unreachable_findings': unreachable_findings,
    }


# --------------------------------------------------------------------------
# Case C03: loops with finite convergence, local rebinding, parameters and
# nested-scope value isolation.
# --------------------------------------------------------------------------

C03_LOOP_SAMPLE = """
    import requests

    def f(items):
        v = False
        for item in items:
            v = True
            requests.get(verify=v)
        requests.get(verify=v)
"""

C03_LOOP_STABLE_SAMPLE = """
    import requests

    def f(items):
        v = False
        for item in items:
            other(item)
        requests.get(verify=v)
"""

C03_ISOLATION_SAMPLE = """
    import requests

    def outer(p):
        v = p

        def inner():
            v = True
            requests.get(verify=v)

        requests.get(verify=v)
    """


def case_c03():
    book = AssertionBookkeeper()

    loop_findings = run_analyzer(C03_LOOP_SAMPLE)
    book.check_equal(
        'switch changed inside loop widens the in-loop call to TOP',
        loop_findings.get(8), (iva.TOP, [])
    )
    book.check_equal(
        'loop exit state is widened, so the later call is also TOP',
        loop_findings.get(9), (iva.TOP, [])
    )

    stable_findings = run_analyzer(C03_LOOP_STABLE_SAMPLE)
    book.check_equal(
        'untouched loop variable retains its pre-loop FALSE binding',
        stable_findings.get(8), (iva.FALSE, [5])
    )

    isolation_findings = run_analyzer(C03_ISOLATION_SAMPLE)
    # Layout: line 8 the inner assignment, 9 the inner call, 11 the outer
    # call. The inner TRUE binding must not escape into the outer function.
    book.check_equal(
        'nested scope rebinding does not leak into the outer scope',
        isolation_findings.get(9), (iva.TRUE, [])
    )
    book.check_equal(
        'outer scope keeps its parameter-derived TOP value independently',
        isolation_findings.get(11), (iva.TOP, [])
    )

    # Parameter isolation: the same name means nothing across functions.
    param_sample = textwrap.dedent("""
        import requests

        def a(v):
            requests.get(verify=v)

        def b():
            v = False
            requests.get(verify=v)
    """)
    param_findings = run_analyzer(param_sample)
    book.check_equal(
        'parameter value starts unknown in its function',
        param_findings.get(5), (iva.TOP, [])
    )
    book.check_equal(
        'another function local of the same name is analyzed separately',
        param_findings.get(9), (iva.FALSE, [8])
    )

    return book, {
        'loop_findings': loop_findings,
        'stable_loop_findings': stable_findings,
        'nested_scope_findings': isolation_findings,
        'parameter_findings': param_findings,
    }


# --------------------------------------------------------------------------
# Case C04: DUO123 identity/position, real call vs assignment locations,
# default-mode compatibility, and unknown never rendered as confirmed safe.
# --------------------------------------------------------------------------

C04_CONFIRMED_SAMPLE = """
    import requests

    def f():
        v = False
        requests.post(verify=v)
"""

C04_UNKNOWN_SAMPLE = """
    import requests

    def decide():
        return False

    def f():
        v = decide()
        requests.get(verify=v)
        requests.get(verify=True)
"""


def case_c04():
    book = AssertionBookkeeper()

    confirmed = run_linter(C04_CONFIRMED_SAMPLE, intraproc_analysis=True)
    book.check(
        'confirmed finding keeps the DUO123 code prefix',
        bool(confirmed) and confirmed[0].message.startswith('DUO123'),
        expected='message starting with DUO123',
        observed=confirmed[0].message if confirmed else None
    )
    book.check_equal(
        'finding position is the real call line, not the assignment line',
        confirmed[0].lineno, 6
    )
    book.check(
        'message cites the assignment line as the value origin',
        'line 5' in confirmed[0].message,
        expected='origin line 5 in message',
        observed=confirmed[0].message
    )

    unknown = run_linter(C04_UNKNOWN_SAMPLE, intraproc_analysis=True)
    book.check_equal(
        'unknown-value call is one review result; all-true call is silent',
        classify_results(unknown), [(9, 'review')]
    )
    book.check(
        'unknown result never claims confirmed insecurity or safety',
        unknown[0].message.startswith(REVIEW_PREFIX),
        expected=REVIEW_PREFIX + ' ...',
        observed=unknown[0].message
    )

    # Default-mode compatibility: identical output with the flag off,
    # including the historical exact template and no review results.
    legacy_direct = run_linter(
        'import requests\nrequests.get(verify=False)\n',
        intraproc_analysis=False
    )
    book.check_equal(
        'legacy mode preserves the exact DUO123 template and call position',
        (legacy_direct[0].lineno, legacy_direct[0].message),
        (2, dlint.linters.BadRequestsUseLinter._error_tmpl)
    )
    legacy_indirect = run_linter(
        C04_UNKNOWN_SAMPLE, intraproc_analysis=False
    )
    book.check_equal(
        'legacy mode never emits review results for variable-backed values',
        classify_results(legacy_indirect), []
    )

    # Unknown-call provenance must not be fabricated as a FALSE origin.
    unknown_states = run_analyzer(C04_UNKNOWN_SAMPLE)
    book.check_equal(
        'analyzer marks unknown-call-derived value TOP with no origins',
        unknown_states.get(9), (iva.TOP, [])
    )

    return book, {
        'confirmed_result': classify_results(confirmed),
        'confirmed_message': confirmed[0].message,
        'unknown_results': classify_results(unknown),
        'legacy_direct': classify_results(legacy_direct),
        'legacy_indirect': classify_results(legacy_indirect),
        'unknown_states': unknown_states,
    }


CASES = (
    ('C01', 'boolean constants, copies, ordering overwrite, requests binding',
     case_c01),
    ('C02', 'static/unknown conditions, merges, early return, unreachability',
     case_c02),
    ('C03', 'loop convergence, rebinding, parameters and nested isolation',
     case_c03),
    ('C04', 'DUO123 identity/positions, default mode, unknown vs confirmed',
     case_c04),
)


def build_record(case_id, description, book, summary, seed):
    return {
        'case': case_id,
        'description': description,
        'seed': seed,
        'assertions': {
            'total': book.passed + book.failed,
            'passed': book.passed,
            'failed': book.failed,
            'skipped': book.skipped,
        },
        'failures': book.failures,
        'summary': summary,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true',
                        help='List available verification cases and exit.')
    parser.add_argument('--case', choices=[case[0] for case in CASES],
                        help='Run one verification case.')
    args = parser.parse_args(argv)

    if args.list:
        for case_id, description, _ in CASES:
            print('{}: {}'.format(case_id, description))
        return 0

    if not args.case:
        parser.error('one of --list or --case is required')

    case_id, description, func = next(
        case for case in CASES if case[0] == args.case
    )
    seed = 'synthetic-source-seed-{}-v1'.format(case_id)

    try:
        book, summary = func()
    except Exception as exc:  # pragma: no cover - reported as failure output
        record = {
            'case': case_id,
            'description': description,
            'seed': seed,
            'assertions': {
                'total': 0, 'passed': 0, 'failed': 1, 'skipped': 0,
            },
            'failures': [{
                'assertion': 'case execution raised an exception',
                'expected': 'case runs to completion',
                'observed': '{}: {}'.format(type(exc).__name__, exc),
            }],
            'summary': {},
        }
        print(json.dumps(record, indent=2, sort_keys=True))
        return 1

    record = build_record(case_id, description, book, summary, seed)
    print(json.dumps(record, indent=2, sort_keys=True))

    totals = record['assertions']
    if (totals['failed'] or totals['skipped'] or totals['total'] == 0):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
