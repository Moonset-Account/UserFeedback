#!/usr/bin/env python

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import importlib
import inspect
import pkgutil
import optparse
import sys

import dlint


class Flake8Extension(object):
    name = dlint.__name__
    version = dlint.__version__

    # These mirror the default 'select' and 'ignore' values flake8 itself
    # uses when the user does not configure those options explicitly.
    DEFAULT_SELECT = ('E', 'W', 'F', 'C')
    DEFAULT_IGNORE = (
        'E121', 'E123', 'E126', 'E226', 'E24', 'E704', 'W503', 'W504'
    )

    def __init__(self, tree, filename, options=None):
        self.tree = tree
        self.filename = filename
        self.options = options

    @classmethod
    def add_options(cls, parser):
        try:
            parser.add_option(
                '--print-dlint-linters',
                action='store_true',
                help='Print Dlint linter information.',
                parse_from_config=False
            )
        except optparse.OptionConflictError:
            # Occurs during development when flake8 detects the dlint package
            # twice: once because it's been installed in editable mode, and
            # once from the local filesystem directory. We can safely nop here
            # since the option(s) have already been added.
            pass

    @classmethod
    def parse_options(cls, options):
        if options.print_dlint_linters:
            code_prefix_len = 7
            linters = Flake8Extension.get_linter_classes()
            output_lines = [
                "{} {} {}".format(l._code, l.__name__, l._error_tmpl[code_prefix_len:])
                for l in sorted(linters, key=lambda li: li._code)
            ]
            print("\n".join(output_lines))
            EX_OK = 0
            sys.exit(EX_OK)

    @classmethod
    def get_plugin_linter_classes(cls):
        module_prefix = 'dlint_plugin_'
        class_prefix = 'Dlint'

        plugin_modules = [
            importlib.import_module(name)
            for finder, name, ispkg in pkgutil.iter_modules()
            if name.startswith(module_prefix)
        ]
        plugin_classes = [
            inner_cls
            for module in plugin_modules
            for name, inner_cls in inspect.getmembers(module, predicate=inspect.isclass)
            if name.startswith(class_prefix)
        ]

        return plugin_classes

    @classmethod
    def get_linter_classes(cls):
        return dlint.linters.ALL + tuple(cls.get_plugin_linter_classes())

    @staticmethod
    def _option_tuple(options, name):
        # flake8 normalizes these options into tuples, but direct callers
        # (and older flake8 versions) may not provide each of them at all.
        value = getattr(options, name, None)
        return tuple(value) if value else ()

    @staticmethod
    def code_matches(code, prefixes):
        # flake8 considers a code selected (or ignored) when either the code
        # itself or one of the configured prefixes is a prefix of the other,
        # e.g. 'DUO105' matches the prefix 'DUO', and 'DUO' matches 'DUO105'.
        return any(
            code.startswith(prefix) or prefix.startswith(code)
            for prefix in prefixes
        )

    @classmethod
    def should_warn(cls, code, options):
        # Without flake8 options (e.g. a direct call in tests) there is no
        # configuration to apply, so the existing default set still runs.
        if options is None:
            return True

        select = cls._option_tuple(options, 'select')
        ignore = cls._option_tuple(options, 'ignore')
        extend_select = cls._option_tuple(options, 'extend_select')
        extend_ignore = cls._option_tuple(options, 'extend_ignore')
        extended_default_select = cls._option_tuple(
            options, 'extended_default_select'
        )
        extended_default_ignore = cls._option_tuple(
            options, 'extended_default_ignore'
        )

        explicitly_selected = bool(select) and select != cls.DEFAULT_SELECT

        if explicitly_selected:
            # An explicit '--select' takes precedence over the default ignore
            # list, and only the selected codes are enabled.
            enabled = (
                cls.code_matches(code, select)
                or cls.code_matches(code, extend_select)
                or cls.code_matches(code, extended_default_select)
            )
        else:
            # Without an explicit selection every code is enabled unless it is
            # ignored through the default, extended-default, or user-provided
            # ignore lists (extended-default selections still opt back in).
            ignored_prefixes = (
                cls.DEFAULT_IGNORE
                + extended_default_ignore
                + ignore
                + extend_ignore
            )
            enabled = not cls.code_matches(code, ignored_prefixes)
            enabled = (
                enabled
                or cls.code_matches(code, extended_default_select)
                or cls.code_matches(code, extend_select)
            )

        # An explicit '--ignore' wins over an explicit '--select', mirroring
        # flake8's own decision order.
        if cls.code_matches(code, ignore) or cls.code_matches(code, extend_ignore):
            enabled = False

        return enabled

    def run(self):
        # Apply the select/ignore decision before visiting the AST so that a
        # linter which will not report never has its visitors execute. This
        # keeps built-in linters and external dlint plugins on the same set of
        # options, instead of running every linter and filtering the output.
        linter_classes = [
            linter_class
            for linter_class in self.get_linter_classes()
            if self.should_warn(linter_class._code, self.options)
        ]

        linter_instances = [linter_class() for linter_class in linter_classes]
        multi_visitor = dlint.multi.MultiNodeVisitor(linter_instances)
        multi_visitor.visit(self.tree)

        for linter_instance in linter_instances:
            for result in linter_instance.get_results():
                yield (
                    result.lineno,
                    result.col_offset,
                    result.message,
                    type(linter_instance)
                )
