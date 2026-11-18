#!/usr/bin/env python

from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import ast

from ... import tree


# Abstract-domain states for the value of a local variable.
#
# BOTTOM: the name has not been bound on any path analyzed so far
# TRUE:   the name is provably the boolean ``True`` on every path
# FALSE:  the name is provably the boolean ``False`` on every path
# NONE:   the name is provably ``None`` on every path
# TOP:    the value cannot be determined (unknown call, unsupported
#         construct, or genuinely different values merge here)
BOTTOM = 'BOTTOM'
TRUE = 'TRUE'
FALSE = 'FALSE'
NONE = 'NONE'
TOP = 'TOP'


class AbstractValue(object):
    """An abstract value pairs a lattice state with provenance information.

    ``origins`` records the assignment statements (their line numbers) that
    produced a FALSE value, so confirmed findings can point the reviewer at
    the source of the insecure value. It is empty for every other state.
    """

    __slots__ = ('state', 'origins')

    def __init__(self, state, origins=None):
        self.state = state
        # A frozenset keeps the value immutable once constructed.
        self.origins = frozenset(origins) if origins else frozenset()

    def __eq__(self, other):
        return (
            isinstance(other, AbstractValue)
            and self.state == other.state
            and self.origins == other.origins
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        if self.origins:
            return 'AbstractValue({}, origins={})'.format(
                self.state, sorted(self.origins)
            )
        return 'AbstractValue({})'.format(self.state)


BOTTOM_VALUE = AbstractValue(BOTTOM)


def join_values(left, right):
    """Least-upper-bound of two abstract values."""
    if left.state == BOTTOM:
        return right
    if right.state == BOTTOM:
        return left
    if left.state == right.state:
        if left.state == FALSE:
            # Provenance from both sides must survive the merge.
            return AbstractValue(FALSE, left.origins | right.origins)
        return left
    # Any disagreement, or a merge involving TOP, collapses to TOP.
    return AbstractValue(TOP)


def join_environments(left, right):
    """Pointwise join of two name -> AbstractValue environments."""
    merged = dict(left)
    for name, value in right.items():
        if name in merged:
            merged[name] = join_values(merged[name], value)
        else:
            merged[name] = value
    return merged


class FlowFinding(object):
    """A ``verify`` keyword classification for one requests call site."""

    __slots__ = ('node', 'state', 'origins')

    def __init__(self, node, state, origins=None):
        self.node = node
        self.state = state
        self.origins = frozenset(origins) if origins else frozenset()


class ScopeAnalyzer(object):
    """Structural intra-procedural dataflow analysis for ``verify=False``.

    The analyzer walks one scope (module or function body) recursively,
    carrying an environment of abstract values. Nested function, class and
    lambda scopes are analyzed in isolation so their locals cannot leak into
    the enclosing scope.

    Results are collected in ``findings`` keyed by the call node: the stored
    value is the join of the abstract ``verify`` values observed on every
    supported path that reaches the call.
    """

    def __init__(self, namespace, bound_locals):
        self.namespace = namespace
        # Names statically bound inside this scope (assignments, parameters,
        # imports, defs, ...). A ``requests``-named binding here shadows the
        # module and must not be treated as the real library. At module scope
        # the import statements themselves are excluded because they *are* the
        # library binding; only a later rebinding shadows it.
        self.bound_locals = frozenset(bound_locals)
        self.findings = {}

    # -- entry point -------------------------------------------------------

    def analyze(self, statements, environment):
        """Analyze a statement list, returning the exit environment.

        A block that ends in ``return``/``raise`` returns the TERMINATED
        sentinel so callers can drop the unreachable continuation.
        """
        current = environment
        for statement in statements:
            current = self.analyze_statement(statement, current)
            if current is TERMINATED:
                return TERMINATED
        return current

    # -- statements --------------------------------------------------------

    def analyze_statement(self, node, environment):
        if isinstance(node, ast.Assign):
            return self.analyze_assign(node, environment)
        if isinstance(node, ast.AnnAssign):
            return self.analyze_ann_assign(node, environment)
        if isinstance(node, ast.AugAssign):
            # Augmented assignment always reads an existing value; treat the
            # result as unknown rather than fabricating precision. The RHS is
            # still evaluated so nested requests calls are detected.
            self.evaluate(node.value, environment)
            return self.analyze_target_unknown(node.target, environment)
        if isinstance(node, ast.If):
            return self.analyze_if(node, environment)
        if isinstance(node, (ast.While, ast.For)):
            return self.analyze_loop(node, environment)
        if isinstance(node, ast.Return):
            if node.value is not None:
                self.evaluate(node.value, environment)
            return TERMINATED
        if isinstance(node, ast.Raise):
            return TERMINATED
        if isinstance(node, ast.Try):
            return self.analyze_try(node, environment)
        if isinstance(node, ast.With):
            return self.analyze_with(node, environment)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            # Nested scope: analyze independently, do not touch our locals.
            self.analyze_nested_scope(node)
            return environment
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return self.analyze_local_import(node, environment)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            # Explicit escape hatch: any affected name is unknown from here
            # on, and globals that alias ``requests`` cannot be trusted.
            result = dict(environment)
            for name in node.names:
                result[name] = AbstractValue(TOP)
            return result
        if isinstance(node, (ast.Pass, ast.Break, ast.Continue)):
            # break/continue cannot be modeled without a full CFG. They only
            # occur inside loops, where affected names are widened to TOP
            # after fixed-point iteration anyway.
            return environment
        if isinstance(node, ast.Expr):
            if node.value is not None:
                self.evaluate(node.value, environment)
            return environment
        if isinstance(node, ast.Delete):
            result = dict(environment)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result.pop(target.id, None)
            return result
        if isinstance(node, ast.Assert):
            if node.test is not None:
                self.evaluate(node.test, environment)
            if node.msg is not None:
                self.evaluate(node.msg, environment)
            return environment

        # Any statement we do not understand is conservatively assumed to
        # mutate local state in ways we cannot track.
        return self.widen_unknown_statement(node, environment)

    def analyze_assign(self, node, environment):
        value = self.evaluate(node.value, environment)
        if value.state == FALSE and not isinstance(node.value, ast.Name):
            # Provenance must identify the assignment statement rather than
            # the constant's own line. A pure copy (RHS is a name) keeps the
            # upstream assignment's provenance instead.
            value = AbstractValue(FALSE, origins=(node.lineno,))
        result = dict(environment)
        for target in node.targets:
            self.bind_target(target, value, result)
        return result

    def analyze_ann_assign(self, node, environment):
        result = dict(environment)
        if node.value is not None:
            value = self.evaluate(node.value, environment)
            if value.state == FALSE and not isinstance(node.value, ast.Name):
                value = AbstractValue(FALSE, origins=(node.lineno,))
            self.bind_target(node.target, value, result)
        return result

    def analyze_target_unknown(self, target, environment):
        result = dict(environment)
        self.bind_target(target, AbstractValue(TOP), result)
        return result

    def analyze_local_import(self, node, environment):
        result = dict(environment)
        for alias in node.names:
            bound_name = alias.asname
            if bound_name is None:
                # 'import foo.bar' binds 'foo'; 'from x import y' binds 'y'.
                if isinstance(node, ast.Import):
                    bound_name = alias.name.split('.')[0]
                else:
                    bound_name = alias.name
            result[bound_name] = AbstractValue(TOP)
        return result

    def analyze_if(self, node, environment):
        # The test expression is evaluated with the environment live on
        # entry; any requests calls nested in it are recorded there.
        condition = self.static_bool(node.test, environment)

        if condition is True:
            return self.analyze(node.body, dict(environment))
        if condition is False:
            if node.orelse:
                return self.analyze(node.orelse, dict(environment))
            return environment

        # Unknown condition: both sides are possible.
        true_env = self.analyze(node.body, dict(environment))
        false_env = (
            self.analyze(node.orelse, dict(environment))
            if node.orelse else dict(environment)
        )

        if true_env is TERMINATED:
            return false_env
        if false_env is TERMINATED:
            return true_env
        return join_environments(true_env, false_env)

    def analyze_loop(self, node, environment):
        # Names assigned anywhere inside the loop body may take a different
        # value on a back edge. We compute a fixed point on the body starting
        # from the loop-entry environment, which is guaranteed to converge
        # because the abstract lattice has finite height.
        if isinstance(node, ast.For):
            entry = dict(environment)
            self.bind_target(node.target, AbstractValue(TOP), entry)
            # The iterable is evaluated in the entry environment, which also
            # records any requests calls nested inside it.
            self.evaluate(node.iter, environment)
        else:
            condition = self.static_bool(node.test, environment)
            self.evaluate(node.test, environment)
            if condition is False:
                # A statically-false loop never runs its body: its calls are
                # unreachable and must not be counted. The else clause, if
                # present, still executes.
                result = dict(environment)
                if node.orelse:
                    else_env = self.analyze(node.orelse, dict(environment))
                    if else_env is not TERMINATED:
                        return else_env
                return result
            entry = dict(environment)

        assigned = sorted(self.assigned_names(node))

        current = entry
        for _ in range(self._fixed_point_limit()):
            body_env = self.analyze(node.body, dict(current))
            if body_env is TERMINATED:
                # An unconditional exit means no back edge reaches the
                # join; loop-iteration effects still apply below.
                body_env = dict(current)
            widened = self.widen_assigned(body_env, current, assigned)
            if self._environments_equal(widened, current):
                current = widened
                break
            current = widened

        # break/continue and the finite-iteration assumption mean every name
        # assigned in the loop must stay unknown after the loop: this keeps
        # the analysis honest about loop-carried values.
        current = self.widen_assigned(current, current, assigned)

        # Calls inside the loop body are likewise reachable on multiple
        # iterations whose environments we deliberately widened, so their
        # findings are widened to the review state as well.
        for call_id, body_node in self._loop_body_call_ids(node):
            finding = self.findings.get(call_id)
            if finding is not None and finding.state != TOP:
                self.findings[call_id] = FlowFinding(body_node, TOP)

        if node.orelse:
            else_env = self.analyze(node.orelse, dict(current))
            if else_env is not TERMINATED:
                current = else_env
        return current

    def analyze_try(self, node, environment):
        # Exceptions make precise flow modeling impossible here. Analyze
        # each clause for its call sites, then widen every name assigned
        # anywhere in the statement to TOP in the exit environment.
        entry = dict(environment)
        self.analyze(node.body, dict(entry))
        for handler in node.handlers:
            clause = dict(entry)
            if handler.type is not None:
                self.evaluate(handler.type, entry)
            if handler.name is not None:
                clause[handler.name] = AbstractValue(TOP)
            self.analyze(handler.body, clause)
        self.analyze(node.orelse, dict(entry))
        self.analyze(node.finalbody, dict(entry))

        result = dict(environment)
        for name in self.assigned_names(node):
            result[name] = AbstractValue(TOP)
        return result

    def analyze_with(self, node, environment):
        result = dict(environment)
        for item in node.items:
            self.evaluate(item.context_expr, result)
            if item.optional_vars is not None:
                self.bind_target(item.optional_vars, AbstractValue(TOP), result)
        return self.analyze(node.body, result)

    def analyze_nested_scope(self, node):
        if isinstance(node, ast.Lambda):
            nested_body = [ast.Return(value=node.body)]
            args = node.args
        elif isinstance(node, ast.ClassDef):
            # Class scope: no parameters, its names never leak out anyway.
            nested = ScopeAnalyzer(
                self.namespace,
                collect_bound_names(node, include_imports=True)
            )
            nested.analyze(node.body, {})
            self.findings = merge_findings(self.findings, nested.findings)
            return
        else:
            nested_body = node.body
            args = node.args

        nested = ScopeAnalyzer(
            self.namespace,
            collect_bound_names(node, include_imports=True)
        )
        nested_env = parameter_environment(args)
        nested.analyze(nested_body, nested_env)
        self.findings = merge_findings(self.findings, nested.findings)

    def widen_unknown_statement(self, node, environment):
        result = dict(environment)
        for name in self.assigned_names(node):
            result[name] = AbstractValue(TOP)
        return result

    def widen_assigned(self, body_env, fallback, assigned):
        result = dict(fallback)
        for name in assigned:
            if name in body_env:
                result[name] = join_values(
                    result.get(name, BOTTOM_VALUE),
                    AbstractValue(TOP)
                )
            else:
                result[name] = AbstractValue(TOP)
        return result

    # -- expressions -------------------------------------------------------

    def evaluate(self, node, environment):
        """Return the AbstractValue of an expression in this environment."""
        if isinstance(node, ast.Call):
            # The call's own value is unknown, but its ``verify`` keyword may
            # be classifiable against the live environment. Arguments are
            # evaluated afterwards so nested calls are not missed.
            if self.is_requests_call(node):
                self.record_call(node, environment)
            for argument in node.args:
                self.evaluate(argument, environment)
            for keyword in node.keywords:
                self.evaluate(keyword.value, environment)
            return AbstractValue(TOP)
        if isinstance(node, ast.Constant):
            if node.value is True:
                return AbstractValue(TRUE)
            if node.value is False:
                return AbstractValue(FALSE, origins=(node.lineno,))
            if node.value is None:
                return AbstractValue(NONE)
            return AbstractValue(TOP)
        # Python 2 compatibility (``ast.NameConstant`` does not exist there).
        if hasattr(ast, 'NameConstant') and isinstance(node, ast.NameConstant):
            if node.value is True:
                return AbstractValue(TRUE)
            if node.value is False:
                return AbstractValue(FALSE, origins=(node.lineno,))
            if node.value is None:
                return AbstractValue(NONE)
            return AbstractValue(TOP)
        if isinstance(node, ast.Name):
            value = environment.get(node.id, BOTTOM_VALUE)
            if value.state == BOTTOM:
                # Unbound on the analyzed path: do not invent precision.
                return AbstractValue(TOP)
            return value
        if isinstance(node, ast.BoolOp):
            return self.evaluate_boolop(node, environment)
        if hasattr(ast, 'NamedExpr') and isinstance(node, ast.NamedExpr):
            # The walrus operator binds a local; mutate the live environment
            # so the binding is visible to later statements on this path.
            value = self.evaluate(node.value, environment)
            if isinstance(node.target, ast.Name):
                environment[node.target.id] = value
            return value
        if isinstance(node, ast.IfExp):
            return self.evaluate_ifexp(node, environment)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self.evaluate_not(node.operand, environment)

        # Any other expression (calls, arithmetic, attributes, ...) cannot
        # be resolved to a static boolean. Visiting it still lets us detect
        # requests calls nested inside the expression.
        self.visit_nested_calls(node, environment)
        return AbstractValue(TOP)

    def evaluate_boolop(self, node, environment):
        # We only model the boolean value itself; short-circuiting
        # assignments do not exist in Python expressions.
        values = [self.evaluate(value, environment) for value in node.values]
        if isinstance(node.op, ast.And):
            result = AbstractValue(TRUE)
            for value in values:
                result = self._bool_and(result, value)
        else:
            result = AbstractValue(FALSE)
            for value in values:
                result = self._bool_or(result, value)
        return result

    @staticmethod
    def _bool_and(left, right):
        if left.state == FALSE or right.state == FALSE:
            return AbstractValue(FALSE, left.origins | right.origins)
        if left.state == TOP or right.state == TOP:
            return AbstractValue(TOP)
        if left.state == TRUE:
            return right
        if right.state == TRUE:
            return left
        return AbstractValue(TOP)

    @staticmethod
    def _bool_or(left, right):
        if left.state == TRUE or right.state == TRUE:
            return AbstractValue(TRUE)
        if left.state == TOP or right.state == TOP:
            return AbstractValue(TOP)
        if left.state == FALSE:
            return right
        if right.state == FALSE:
            return left
        return AbstractValue(TOP)

    def evaluate_not(self, operand, environment):
        value = self.evaluate(operand, environment)
        if value.state == TRUE:
            return AbstractValue(FALSE)
        if value.state == FALSE:
            return AbstractValue(TRUE)
        return AbstractValue(TOP)

    def evaluate_ifexp(self, node, environment):
        condition = self.static_bool(node.test, environment)
        if condition is True:
            return self.evaluate(node.body, environment)
        if condition is False:
            return self.evaluate(node.orelse, environment)
        return join_values(
            self.evaluate(node.body, environment),
            self.evaluate(node.orelse, environment)
        )

    def static_bool(self, node, environment):
        """Return True/False when the test is statically decidable, else None."""
        value = self.evaluate(node, environment)
        if value.state == TRUE:
            return True
        if value.state == FALSE:
            return False
        # NONE and non-boolean abstract values are truthy/falsy in Python but
        # the linter's domain is boolean verification flags; stay unknown.
        return None

    # -- target binding ----------------------------------------------------

    def bind_target(self, target, value, result):
        if isinstance(target, ast.Name):
            result[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self.bind_target(element, value, result)
            return
        if isinstance(target, ast.Starred):
            self.bind_target(target.value, value, result)
            return
        # Attribute/subscript targets mutate objects we cannot model; mark
        # every involved local name unknown.
        for name_node in self.walk_names(target):
            result[name_node.id] = AbstractValue(TOP)

    # -- requests call detection ------------------------------------------

    def visit_nested_calls(self, node, environment):
        # Called while evaluating an unsupported expression; every requests
        # call nested within it (including the expression root when it is
        # itself a call) is reachable with the current environment.
        for call in self._iter_same_scope_calls(node):
            if self.is_requests_call(call):
                self.record_call(call, environment)

    def is_requests_call(self, call):
        func = call.func
        if isinstance(func, ast.Name):
            # 'from requests import get' style: ask the namespace whether the
            # imported name really is one of the requests entry points.
            return self._imported_name_is_requests(func.id)
        if not isinstance(func, ast.Attribute):
            return False
        if not isinstance(func.value, ast.Name):
            return False
        root = func.value.id
        if root in self.bound_locals:
            # A local shadows the import, e.g. a custom object named
            # ``requests``: it is not the library.
            return False
        module_path = tree.module_path_str(func)
        return self.namespace.illegal_module_imported(
            module_path, 'requests.' + func.attr
        )

    def _imported_name_is_requests(self, name):
        entry_points = (
            'request', 'get', 'options', 'head', 'post',
            'put', 'patch', 'delete',
        )
        return any(
            self.namespace.illegal_module_imported(
                name, 'requests.' + entry
            )
            for entry in entry_points
        )

    def record_call(self, call, environment):
        verify_value = None
        has_double_star = False
        for keyword in call.keywords:
            if keyword.arg is None:
                # **kwargs could supply verify itself.
                has_double_star = True
                continue
            if keyword.arg == 'verify':
                verify_value = self.evaluate(keyword.value, environment)

        if verify_value is None:
            if has_double_star:
                verify_value = AbstractValue(TOP)
            else:
                # No explicit verify: requests defaults to verification on,
                # i.e. not a risk and not something to ask reviewers about.
                return

        # The same call node can be analyzed several times: once per loop
        # iteration in the fixed-point computation, and whenever an
        # enclosing expression is re-evaluated. Joining every observation
        # yields the value that holds across all analyzed paths; the lattice
        # is idempotent so a genuine re-evaluation in an unchanged
        # environment does not perturb the result.
        previous = self.findings.get(id(call))
        if previous is None:
            self.findings[id(call)] = FlowFinding(
                call, verify_value.state, verify_value.origins
            )
        else:
            merged = join_values(
                AbstractValue(previous.state, previous.origins),
                verify_value
            )
            self.findings[id(call)] = FlowFinding(
                call, merged.state, merged.origins
            )

    # -- AST walking helpers ----------------------------------------------

    def _iter_same_scope_calls(self, node):
        if isinstance(node, ast.Call):
            yield node
        for child in _iter_same_scope_nodes(node):
            if isinstance(child, ast.Call):
                yield child

    @staticmethod
    def walk_names(node):
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                yield child

    def assigned_names(self, node):
        return {
            child.id
            for child in _iter_same_scope_nodes(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
        }

    def _loop_body_call_ids(self, loop_node):
        """Return ``(node id, node)`` for calls in the loop body, excluding
        calls nested inside further nested scopes."""
        for statement in loop_node.body:
            for child in _iter_same_scope_nodes(statement):
                if isinstance(child, ast.Call):
                    yield id(child), child

    # -- fixed-point bookkeeping ------------------------------------------

    @staticmethod
    def _fixed_point_limit():
        # The lattice has five states; even with environment growth this is
        # a generous bound on monotone iterations per loop.
        return 64

    @staticmethod
    def _environments_equal(left, right):
        if set(left) != set(right):
            return False
        return all(left[name] == right[name] for name in left)


class _Terminated(object):
    """Sentinel marking that a block ended with return/raise."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(_Terminated, cls).__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'TERMINATED'


TERMINATED = _Terminated()


NESTED_SCOPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda
)


def _iter_same_scope_nodes(node):
    """Yield descendant nodes without crossing nested scope boundaries."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, NESTED_SCOPES):
            continue
        yield child
        for inner in _iter_same_scope_nodes(child):
            yield inner


def parameter_environment(arguments):
    """Build the entry environment of a function from its parameters.

    Parameter values come from callers we do not analyze, so every
    parameter starts TOP. Positional-only, keyword-only and vararg/varkw
    parameters are all handled uniformly.
    """
    environment = {}
    arg_nodes = list(arguments.args)
    if getattr(arguments, 'posonlyargs', None):
        arg_nodes = list(arguments.posonlyargs) + arg_nodes
    arg_nodes.extend(arguments.kwonlyargs)
    if arguments.vararg is not None:
        arg_nodes.append(arguments.vararg)
    if arguments.kwarg is not None:
        arg_nodes.append(arguments.kwarg)
    for arg in arg_nodes:
        environment[arg.arg] = AbstractValue(TOP)
    return environment


def collect_bound_names(node, include_imports=True):
    """Collect every name statically bound within a scope node.

    When ``include_imports`` is False the module-level ``import``/``from ...
    import`` bindings are omitted: they are the library imports themselves
    rather than shadowing rebindings. Function- and class-level imports are
    always included because they bind locals that hide the module-level
    import.
    """
    is_module_scope = isinstance(node, ast.Module)
    names = set()
    for child in _iter_same_scope_nodes(node):
        if isinstance(child, NESTED_SCOPES):
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, ast.Import):
            if include_imports or not is_module_scope:
                for alias in child.names:
                    names.add(
                        alias.asname
                        if alias.asname else alias.name.split('.')[0]
                    )
        elif isinstance(child, ast.ImportFrom):
            if include_imports or not is_module_scope:
                for alias in child.names:
                    names.add(alias.asname if alias.asname else alias.name)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
    # Parameters are bindings of this scope too.
    args = getattr(node, 'args', None)
    if args is not None:
        arg_nodes = list(args.args)
        if getattr(args, 'posonlyargs', None):
            arg_nodes = list(args.posonlyargs) + arg_nodes
        arg_nodes.extend(args.kwonlyargs)
        if args.vararg is not None:
            arg_nodes.append(args.vararg)
        if args.kwarg is not None:
            arg_nodes.append(args.kwarg)
        for arg in arg_nodes:
            names.add(arg.arg)
    return names


def merge_findings(existing, nested):
    merged = dict(existing)
    for key, finding in nested.items():
        if key in merged:
            joined = join_values(
                AbstractValue(merged[key].state, merged[key].origins),
                AbstractValue(finding.state, finding.origins)
            )
            merged[key] = FlowFinding(
                finding.node, joined.state, joined.origins
            )
        else:
            merged[key] = finding
    return merged


def analyze_module(module_node, namespace):
    """Run the intra-procedural analysis over a whole module.

    Returns a dict keyed by call-node id with :class:`FlowFinding` values.
    """
    analyzer = ScopeAnalyzer(
        namespace,
        collect_bound_names(module_node, include_imports=False)
    )
    analyzer.analyze(module_node.body, {})
    return analyzer.findings
