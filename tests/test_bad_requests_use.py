#!/usr/bin/env python

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import unittest

import dlint


class TestBadRequestsUse(dlint.test.base.BaseTest):

    def test_bad_requests_usage(self):
        python_node = self.get_ast_node(
            """
            import requests

            requests.request(verify=False)
            requests.get(verify=False)
            requests.options(verify=False)
            requests.head(verify=False)
            requests.post(verify=False)
            requests.put(verify=False)
            requests.patch(verify=False)
            requests.delete(verify=False)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter()
        linter.visit(python_node)

        result = linter.get_results()
        expected = [
            dlint.linters.base.Flake8Result(
                lineno=4,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=5,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=6,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=7,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=8,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=9,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=10,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
            dlint.linters.base.Flake8Result(
                lineno=11,
                col_offset=0,
                message=dlint.linters.BadRequestsUseLinter._error_tmpl
            ),
        ]

        assert result == expected

    def test_legacy_mode_ignores_local_assignments(self):
        python_node = self.get_ast_node(
            """
            import requests

            def f():
                v = False
                requests.get(verify=v)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter()
        linter.visit(python_node)

        # Default mode keeps the historical direct-keyword behavior: a
        # variable-backed value is not detected.
        assert linter.get_results() == []

    def test_intraproc_mode_confirms_local_false_assignment(self):
        python_node = self.get_ast_node(
            """
            import requests

            def f():
                v = False
                requests.get(verify=v)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        result = linter.get_results()
        expected = [
            dlint.linters.base.Flake8Result(
                lineno=6,
                col_offset=4,
                message=(
                    'DUO123 use of "verify=False" is insecure in '
                    '"requests" module (value assigned at line 5)'
                )
            )
        ]

        assert result == expected

    def test_intraproc_mode_confirms_branch_overwrite(self):
        python_node = self.get_ast_node(
            """
            import requests

            def f(flag):
                v = True
                if flag:
                    v = False
                requests.get(verify=v)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        result = linter.get_results()
        expected = [
            dlint.linters.base.Flake8Result(
                lineno=8,
                col_offset=4,
                message=(
                    'DUO123 unable to determine "verify" value in '
                    '"requests" module call, manual review required'
                )
            )
        ]

        assert result == expected

    def test_intraproc_mode_early_return_isolates_insecure_path(self):
        python_node = self.get_ast_node(
            """
            import requests

            def f(flag):
                v = True
                if flag:
                    return
                requests.get(verify=v)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        result = linter.get_results()

        # Only the fall-through path reaches the call, and on it ``v`` is
        # still True: no risk result of any kind is emitted.
        assert result == []

    def test_intraproc_mode_all_true_is_silent(self):
        python_node = self.get_ast_node(
            """
            import requests

            def f():
                v = True
                requests.get(verify=v)
                requests.get(verify=True)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        assert linter.get_results() == []

    def test_intraproc_mode_unknown_is_not_confirmed_safe(self):
        python_node = self.get_ast_node(
            """
            import requests

            def decide():
                return False

            def f():
                v = decide()
                requests.get(verify=v)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        result = linter.get_results()

        assert len(result) == 1
        assert result[0].lineno == 9
        assert 'unable to determine' in result[0].message
        assert result[0].message != dlint.linters.BadRequestsUseLinter._error_tmpl

    def test_intraproc_mode_skips_custom_same_named_object(self):
        python_node = self.get_ast_node(
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

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        assert linter.get_results() == []

    def test_intraproc_mode_unreachable_call_not_counted(self):
        python_node = self.get_ast_node(
            """
            import requests

            def f():
                return
                requests.get(verify=False)
            """
        )

        linter = dlint.linters.BadRequestsUseLinter(intraproc_analysis=True)
        linter.visit(python_node)

        assert linter.get_results() == []


if __name__ == "__main__":
    unittest.main()
