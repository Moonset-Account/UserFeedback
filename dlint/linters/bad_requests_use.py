#!/usr/bin/env python

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import ast

from .helpers import bad_kwarg_use
from .helpers import intraproc_verify_analysis

from .. import tree


class BadRequestsUseLinter(bad_kwarg_use.BadKwargUseLinter):
    """This linter looks for use of the "verify=False" kwarg when using the
    "requests" module. SSL verification is good, use SSL verification.

    http://docs.python-requests.org/en/master/user/advanced/#ssl-cert-verification

    By default only the legacy direct-keyword detection runs. Passing
    ``intraproc_analysis=True`` enables an optional intra-procedural
    dataflow analysis that also understands locally assigned values:

    * boolean constants and variable copies
    * sequential assignment and later reassignment ("ordering overwrite")
    * statically decidable ``if``/``else`` branches and early ``return``
    * loops, whose state is widened to a finite fixed point

    A call is reported as a confirmed DUO123 finding only when every
    supported path reaching the call proves ``verify`` is ``False``. When
    true and false paths merge, or the value comes from an unsupported
    construct/unknown call, a separate "needs review" result is emitted
    instead. All-true (or default) calls produce no risk result, and
    unreachable calls are never counted.
    """
    off_by_default = False

    _code = 'DUO123'
    _error_tmpl = 'DUO123 use of "verify=False" is insecure in "requests" module'
    _review_tmpl = (
        'DUO123 unable to determine "verify" value in "requests" '
        'module call, manual review required'
    )
    _origin_tmpl = ' (value assigned at line {})'

    def __init__(self, *args, **kwargs):
        # Opt-in flag: default mode keeps the historical behavior untouched.
        self.intraproc_analysis = kwargs.pop('intraproc_analysis', False)
        super(BadRequestsUseLinter, self).__init__(*args, **kwargs)

    @property
    def kwargs(self):
        return [
            {
                "module_path": "requests.request",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.get",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.options",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.head",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.post",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.put",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.patch",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
            {
                "module_path": "requests.delete",
                "kwarg_name": "verify",
                "predicate": tree.kwarg_false,
            },
        ]

    def visit(self, node):
        if not self.intraproc_analysis:
            # Legacy behavior: direct "verify=False" keyword detection only.
            super(BadRequestsUseLinter, self).visit(node)
            return

        if not self.namespace:
            from .. import namespace as namespace_module
            self.namespace = namespace_module.Namespace.from_module_node(node)

        if not isinstance(node, ast.Module):
            # The analysis is module-driven; non-module roots (used in some
            # unit tests) fall back to the visitor-based detection.
            super(BadRequestsUseLinter, self).visit(node)
            return

        self._run_intraproc_analysis(node)

    def visit_Module(self, node):
        if not self.intraproc_analysis:
            # The base linters rely on generic_visit; they do not define
            # visit_Module themselves.
            self.generic_visit(node)
            return

        # MultiNodeVisitor dispatches visit_* methods directly rather than
        # going through visit(), so the analysis must be hooked here as well.
        self._run_intraproc_analysis(node)
        # Skip generic_visit: the analyzer already covered every scope, and
        # the visitor-based fallback would double-report direct keywords.

    def _run_intraproc_analysis(self, node):
        findings = intraproc_verify_analysis.analyze_module(
            node, self.namespace
        )

        # Imported lazily to avoid a circular import while the ``dlint``
        # package is still initializing.
        from . import base as linter_base

        for finding in findings.values():
            if finding.state == intraproc_verify_analysis.FALSE:
                message = self._confirmed_message(finding)
                self.results.append(
                    linter_base.Flake8Result(
                        lineno=finding.node.lineno,
                        col_offset=finding.node.col_offset,
                        message=message
                    )
                )
            elif finding.state == intraproc_verify_analysis.TOP:
                # Unknown must never be presented as confirmed safety: emit
                # a distinct, independently classifiable review result.
                self.results.append(
                    linter_base.Flake8Result(
                        lineno=finding.node.lineno,
                        col_offset=finding.node.col_offset,
                        message=self._review_tmpl
                    )
                )
            # TRUE/NONE: verification provably enabled, no risk to report.

    def _confirmed_message(self, finding):
        origins = sorted(finding.origins)
        if not origins:
            return self._error_tmpl
        return self._error_tmpl + self._origin_tmpl.format(
            ', '.join(str(line) for line in origins)
        )
