#!/usr/bin/env python

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import unittest

import dlint

from dlint import namespace
from dlint.linters.helpers import intraproc_verify_analysis as iva


class TestIntraprocVerifyAnalysis(dlint.test.base.BaseTest):

    def analyze(self, source):
        node = self.get_ast_node(source)
        ns = namespace.Namespace.from_module_node(node)
        findings = iva.analyze_module(node, ns)
        return {
            finding.node.lineno: (finding.state, sorted(finding.origins))
            for finding in findings.values()
        }

    def test_lattice_join(self):
        bottom = iva.BOTTOM_VALUE
        true = iva.AbstractValue(iva.TRUE)
        false1 = iva.AbstractValue(iva.FALSE, origins=(1,))
        false2 = iva.AbstractValue(iva.FALSE, origins=(2,))
        top = iva.AbstractValue(iva.TOP)

        self.assertEqual(
            iva.join_values(bottom, true), true
        )
        self.assertEqual(
            iva.join_values(true, bottom), true
        )
        self.assertEqual(
            iva.join_values(false1, false2),
            iva.AbstractValue(iva.FALSE, origins=(1, 2))
        )
        self.assertEqual(
            iva.join_values(true, false1).state, iva.TOP
        )
        self.assertEqual(
            iva.join_values(top, false1).state, iva.TOP
        )

    def test_environment_join_is_pointwise(self):
        left = {'v': iva.AbstractValue(iva.FALSE, origins=(1,))}
        right = {
            'v': iva.AbstractValue(iva.FALSE, origins=(2,)),
            'w': iva.AbstractValue(iva.TRUE),
        }

        joined = iva.join_environments(left, right)

        self.assertEqual(joined['w'].state, iva.TRUE)
        self.assertEqual(
            joined['v'], iva.AbstractValue(iva.FALSE, origins=(1, 2))
        )

    def test_boolean_constants_classify(self):
        findings = self.analyze(
            """
            import requests

            requests.get(verify=False)
            requests.get(verify=True)
            requests.get()
            """
        )

        self.assertEqual(
            findings,
            {
                4: (iva.FALSE, [4]),
                5: (iva.TRUE, []),
            }
        )

    def test_variable_copy_preserves_value_and_origin(self):
        findings = self.analyze(
            """
            import requests

            def f():
                v = False
                w = v
                requests.get(verify=w)
            """
        )

        # The copy keeps the FALSE state and the original assignment's line.
        self.assertEqual(findings[7], (iva.FALSE, [5]))

    def test_later_rebinding_overwrites_in_order(self):
        findings = self.analyze(
            """
            import requests

            def f():
                v = False
                requests.get(verify=v)
                v = True
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[6], (iva.FALSE, [5]))
        self.assertEqual(findings[8], (iva.TRUE, []))

    def test_statically_known_condition_takes_single_branch(self):
        findings = self.analyze(
            """
            import requests

            def f():
                if True:
                    requests.get(verify=False)
                else:
                    requests.get(verify=True)
            """
        )

        self.assertEqual(findings[6], (iva.FALSE, [6]))
        self.assertNotIn(8, findings)

    def test_unknown_condition_merges_both_branches(self):
        findings = self.analyze(
            """
            import requests

            def f(flag):
                if flag:
                    v = False
                else:
                    v = True
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[9], (iva.TOP, []))

    def test_early_return_isolates_insecure_branch(self):
        findings = self.analyze(
            """
            import requests

            def f(flag):
                if flag:
                    return
                requests.get(verify=False)
            """
        )

        self.assertEqual(findings[7], (iva.FALSE, [7]))

    def test_unreachable_call_is_not_recorded(self):
        findings = self.analyze(
            """
            import requests

            def f():
                return
                requests.get(verify=False)
            """
        )

        self.assertEqual(findings, {})

    def test_loop_widens_mutated_switch_to_unknown(self):
        findings = self.analyze(
            """
            import requests

            def f(items):
                v = False
                for item in items:
                    v = True
                    requests.get(verify=v)
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[8], (iva.TOP, []))
        self.assertEqual(findings[9], (iva.TOP, []))

    def test_loop_converges_without_mutation(self):
        findings = self.analyze(
            """
            import requests

            def f(items):
                v = False
                for item in items:
                    requests.get(verify=v)
            """
        )

        # Loop-carried reachability is widened even when no local statement
        # mutates the switch: fixed-point convergence bounds the work, but
        # the in-loop call stays a reviewable result rather than confirmed.
        self.assertEqual(findings[7], (iva.TOP, []))

    def test_call_after_converged_loop_uses_exit_state(self):
        findings = self.analyze(
            """
            import requests

            def f(items):
                v = False
                for item in items:
                    other(item)
                requests.get(verify=v)
            """
        )

        # No assignment touches ``v`` inside the loop, so the exit state
        # retains the FALSE binding from before the loop.
        self.assertEqual(findings[8], (iva.FALSE, [5]))

    def test_parameters_start_unknown(self):
        findings = self.analyze(
            """
            import requests

            def f(v):
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[5], (iva.TOP, []))

    def test_nested_scope_locals_do_not_leak(self):
        findings = self.analyze(
            """
            import requests

            def f():
                v = False

                def inner():
                    v = True
                    requests.get(verify=v)

                requests.get(verify=v)
            """
        )

        # The nested call sees the inner rebinding, the outer call the
        # outer value: neither scope borrows the other's binding.
        self.assertEqual(findings[9], (iva.TRUE, []))
        self.assertEqual(findings[11], (iva.FALSE, [5]))

    def test_locally_rebound_requests_name_is_not_target(self):
        findings = self.analyze(
            """
            import requests

            class Fake(object):
                def get(self, **kwargs):
                    return None

            def f():
                requests = Fake()
                requests.get(verify=False)
            """
        )

        self.assertEqual(findings, {})

    def test_unknown_call_result_is_top(self):
        findings = self.analyze(
            """
            import requests

            def decide():
                return False

            def f():
                v = decide()
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[9], (iva.TOP, []))

    def test_aliased_import_is_still_target(self):
        findings = self.analyze(
            """
            import requests as req

            def f():
                req.get(verify=False)
            """
        )

        self.assertEqual(findings[5], (iva.FALSE, [5]))

    def test_from_import_is_still_target(self):
        findings = self.analyze(
            """
            from requests import get

            def f():
                get(verify=False)
            """
        )

        self.assertEqual(findings[5], (iva.FALSE, [5]))

    def test_unsupported_try_keeps_unknown(self):
        findings = self.analyze(
            """
            import requests

            def f():
                v = False
                try:
                    v = decide()
                except Exception:
                    pass
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[10], (iva.TOP, []))

    def test_double_star_kwargs_is_unknown(self):
        findings = self.analyze(
            """
            import requests

            def f(**kwargs):
                requests.get(**kwargs)
            """
        )

        self.assertEqual(findings[5], (iva.TOP, []))

    def test_static_false_while_body_is_unreachable(self):
        findings = self.analyze(
            """
            import requests

            def f():
                while False:
                    requests.get(verify=False)
                requests.get(verify=True)
            """
        )

        # The loop body never executes; only the post-loop call is recorded.
        self.assertNotIn(6, findings)
        self.assertEqual(findings[7], (iva.TRUE, []))

    def test_walrus_assignment_is_tracked_in_live_environment(self):
        findings = self.analyze(
            """
            import requests

            def f():
                if (v := False):
                    pass
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[7], (iva.FALSE, [5]))

    def test_not_of_constant_is_tracked(self):
        findings = self.analyze(
            """
            import requests

            def f():
                v = not True
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[6], (iva.FALSE, [5]))

    def test_chained_assignment_binds_every_target(self):
        findings = self.analyze(
            """
            import requests

            def f():
                a = b = False
                requests.get(verify=a)
                requests.get(verify=b)
            """
        )

        self.assertEqual(findings[6], (iva.FALSE, [5]))
        self.assertEqual(findings[7], (iva.FALSE, [5]))

    def test_static_conditional_expression_takes_one_branch(self):
        findings = self.analyze(
            """
            import requests

            def f(flag):
                v = False if flag else True
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[6], (iva.TOP, []))

        static_findings = self.analyze(
            """
            import requests

            def f():
                v = False if True else True
                requests.get(verify=v)
            """
        )

        self.assertEqual(static_findings[6], (iva.FALSE, [5]))

    def test_deleted_name_does_not_keep_stale_precision(self):
        findings = self.analyze(
            """
            import requests

            def f():
                v = False
                del v
                requests.get(verify=v)
            """
        )

        self.assertEqual(findings[7], (iva.TOP, []))

    def test_module_level_flow_uses_live_value(self):
        findings = self.analyze(
            """
            import requests

            v = False
            if True:
                w = v
            else:
                w = True
            requests.get(verify=w)
            """
        )

        self.assertEqual(findings[9], (iva.FALSE, [4]))


if __name__ == "__main__":
    unittest.main()
