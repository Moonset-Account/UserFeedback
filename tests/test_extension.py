#!/usr/bin/env python

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import os
import sys
import unittest

# Since extension imports dlint we cannot add it to the module or else we'll
# have circular imports. Thus we must come up with some tricks to import it
sys.path.append(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "dlint"
    )
)
sys.path.append(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "dlint",
        "test"
    )
)

import base  # noqa: E402
import extension  # noqa: E402


class Options(object):
    """A minimal stand-in for flake8's parsed option object."""

    def __init__(self, select=None, ignore=None, extend_select=None,
                 extend_ignore=None, extended_default_select=None,
                 extended_default_ignore=None):
        self.select = select
        self.ignore = ignore
        self.extend_select = extend_select
        self.extend_ignore = extend_ignore
        self.extended_default_select = extended_default_select
        self.extended_default_ignore = extended_default_ignore


class TestFlake8Extension(base.BaseTest):

    def test_flake8_extension_empty(self):
        python_node = self.get_ast_node(
            """
            """
        )

        linter = extension.Flake8Extension(python_node, "unused")

        result = list(linter.run())
        expected = []

        assert result == expected

    def test_flake8_extension_basic(self):
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        linter = extension.Flake8Extension(python_node, "unused")

        result = list(linter.run())
        expected = [
            (
                2,
                0,
                extension.dlint.linters.BadExecUseLinter._error_tmpl,
                extension.dlint.linters.BadExecUseLinter
            )
        ]

        assert result == expected

    def test_flake8_extension_direct_call_uses_default_set(self):
        # A direct call without options keeps the historical behavior:
        # every linter in the default set is eligible to run.
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        linter = extension.Flake8Extension(python_node, "unused")

        result = list(linter.run())

        assert len(result) == 1
        assert result[0][3] is extension.dlint.linters.BadExecUseLinter

    def test_should_warn_without_options_runs_everything(self):
        linter_classes = extension.Flake8Extension.get_linter_classes()

        result = [
            linter_class._code
            for linter_class in linter_classes
            if extension.Flake8Extension.should_warn(
                linter_class._code, None
            )
        ]

        expected = [linter_class._code for linter_class in linter_classes]

        assert result == expected

    def test_should_warn_select_prefix(self):
        # An explicit selection only enables the matching codes.
        options = Options(select=('DUO105',))

        assert extension.Flake8Extension.should_warn('DUO105', options)
        assert not extension.Flake8Extension.should_warn('DUO104', options)

    def test_should_warn_select_prefix_family(self):
        # Selecting the shared prefix enables the whole family.
        options = Options(select=('DUO',))

        assert extension.Flake8Extension.should_warn('DUO105', options)
        assert extension.Flake8Extension.should_warn('DUO138', options)

    def test_should_warn_select_other_prefix_disabled(self):
        # A non-default selection that does not include the dlint prefix
        # disables every dlint linter.
        options = Options(select=('E100',))

        assert not extension.Flake8Extension.should_warn('DUO105', options)

    def test_should_warn_default_select_does_not_narrow(self):
        # flake8's own default selection is not an explicit narrowing, so the
        # dlint default set still runs.
        options = Options(
            select=extension.Flake8Extension.DEFAULT_SELECT
        )

        assert extension.Flake8Extension.should_warn('DUO105', options)

    def test_should_warn_explicit_select_overrides_default_ignore(self):
        # The default ignore list must not suppress an explicitly selected
        # code even when the ignored prefix would otherwise match.
        options = Options(
            select=('DUO105',),
            ignore=extension.Flake8Extension.DEFAULT_IGNORE
        )

        assert extension.Flake8Extension.should_warn('DUO105', options)

    def test_should_warn_ignore_takes_precedence_over_select(self):
        options = Options(select=('DUO',), ignore=('DUO105',))

        assert not extension.Flake8Extension.should_warn('DUO105', options)
        assert extension.Flake8Extension.should_warn('DUO104', options)

    def test_should_warn_extend_ignore_takes_precedence_over_select(self):
        options = Options(select=('DUO',), extend_ignore=('DUO105',))

        assert not extension.Flake8Extension.should_warn('DUO105', options)
        assert extension.Flake8Extension.should_warn('DUO104', options)

    def test_should_warn_no_select_applies_ignore(self):
        # Without an explicit selection the ignore list filters the otherwise
        # fully enabled default set.
        options = Options(ignore=('DUO105',))

        assert not extension.Flake8Extension.should_warn('DUO105', options)
        assert extension.Flake8Extension.should_warn('DUO104', options)

    def test_should_warn_extend_select_without_select(self):
        options = Options(extend_select=('DUO105',))

        assert extension.Flake8Extension.should_warn('DUO105', options)
        assert extension.Flake8Extension.should_warn('DUO104', options)

    def test_flake8_extension_select_only_runs_selected_linter(self):
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        options = Options(select=('DUO105',))
        linter = extension.Flake8Extension(python_node, "unused", options)

        result = list(linter.run())
        expected = [
            (
                2,
                0,
                extension.dlint.linters.BadExecUseLinter._error_tmpl,
                extension.dlint.linters.BadExecUseLinter
            )
        ]

        assert result == expected

    def test_flake8_extension_select_unrelated_code_collects_nothing(self):
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        options = Options(select=('DUO104',))
        linter = extension.Flake8Extension(python_node, "unused", options)

        result = list(linter.run())
        expected = []

        assert result == expected

    def test_flake8_extension_ignore_suppresses_linter(self):
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        options = Options(select=('DUO',), ignore=('DUO105',))
        linter = extension.Flake8Extension(python_node, "unused", options)

        result = list(linter.run())
        expected = []

        assert result == expected

    def test_flake8_extension_two_independent_configurations(self):
        # The same AST is checked under two configurations in the same
        # process: each run must collect results according to its own rules
        # without leaking state into the other run.
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        first_options = Options(select=('DUO104',))
        first_linter = extension.Flake8Extension(
            python_node, "unused", first_options
        )

        second_options = Options(select=('DUO105',))
        second_linter = extension.Flake8Extension(
            python_node, "unused", second_options
        )

        first_result = list(first_linter.run())
        second_result = list(second_linter.run())

        assert first_result == []
        assert second_result == [
            (
                2,
                0,
                extension.dlint.linters.BadExecUseLinter._error_tmpl,
                extension.dlint.linters.BadExecUseLinter
            )
        ]

    def test_flake8_extension_does_not_visit_filtered_linter(self):
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        options = Options(select=('DUO104',))
        linter = extension.Flake8Extension(python_node, "unused", options)

        selected_classes = [
            linter_class
            for linter_class in linter.get_linter_classes()
            if linter.should_warn(linter_class._code, options)
        ]

        assert extension.dlint.linters.BadExecUseLinter not in selected_classes

        result = list(linter.run())

        assert result == []

    def test_flake8_extension_filtered_linter_visitor_never_executes(self):
        python_node = self.get_ast_node(
            """
            exec('print "TEST"')
            """
        )

        visited = []

        class DlintRecordingLinter(
            extension.dlint.linters.base.BaseLinter
        ):
            _code = 'DUO998'
            _error_tmpl = 'DUO998 recording linter'

            def visit_Module(self, node):
                visited.append(True)
                self.results.append(
                    extension.dlint.linters.base.Flake8Result(
                        lineno=1,
                        col_offset=0,
                        message=self._error_tmpl
                    )
                )

        original_get_plugin_linter_classes = (
            extension.Flake8Extension.get_plugin_linter_classes
        )
        extension.Flake8Extension.get_plugin_linter_classes = classmethod(
            lambda cls: [DlintRecordingLinter]
        )
        try:
            # Ignored: the linter's visitors must not execute at all, even
            # though the AST contains a node it would normally inspect.
            ignored_linter = extension.Flake8Extension(
                python_node,
                "unused",
                Options(ignore=('DUO998',))
            )

            ignored_result = list(ignored_linter.run())

            assert visited == []
            assert all(
                finding[3] is not DlintRecordingLinter
                for finding in ignored_result
            )

            # Selected: the very same linter executes and reports.
            selected_linter = extension.Flake8Extension(
                python_node,
                "unused",
                Options(select=('DUO998',))
            )

            selected_result = list(selected_linter.run())

            assert visited == [True]
            assert selected_result == [
                (1, 0, DlintRecordingLinter._error_tmpl, DlintRecordingLinter)
            ]
        finally:
            extension.Flake8Extension.get_plugin_linter_classes = (
                original_get_plugin_linter_classes
            )

    def test_flake8_extension_respects_external_plugin_options(self):
        # An external dlint plugin (discovered via the 'dlint_plugin_' module
        # naming convention) must be gated by the same select/ignore options
        # as the built-in linters.
        code = 'DUO999'

        class DlintExternalPluginLinter(
            extension.dlint.linters.base.BaseLinter
        ):
            _code = code
            _error_tmpl = 'DUO999 external plugin finding'

        original_get_plugin_linter_classes = (
            extension.Flake8Extension.get_plugin_linter_classes
        )
        extension.Flake8Extension.get_plugin_linter_classes = classmethod(
            lambda cls: [DlintExternalPluginLinter]
        )
        try:
            selected_options = Options(select=(code,))
            ignored_options = Options(select=('DUO',), ignore=(code,))
            unrelated_options = Options(select=('DUO105',))

            assert extension.Flake8Extension.should_warn(
                code, selected_options
            )
            assert not extension.Flake8Extension.should_warn(
                code, ignored_options
            )
            assert not extension.Flake8Extension.should_warn(
                code, unrelated_options
            )
        finally:
            extension.Flake8Extension.get_plugin_linter_classes = (
                original_get_plugin_linter_classes
            )


if __name__ == "__main__":
    unittest.main()
