"""Abstract interpreter and cross-procedure tenant-provenance engine."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .core import (
    BOTTOM,
    NONE,
    NodeRef,
    Loss,
    PVal,
    alt_value,
    join_values,
    literal_value,
    tenant_value,
    tuple_value,
    unknown_value,
    with_ref,
)
from .program import Program, ProgramValidationError


UNINIT = PVal("uninit")


@dataclass
class Env:
    vars: dict[str, PVal] = field(default_factory=dict)
    reachable: bool = True

    def copy(self) -> "Env":
        return Env(dict(self.vars), self.reachable)

    def get(self, name: str) -> PVal:
        return self.vars.get(name, UNINIT)

    def set(self, name: str, value: PVal) -> None:
        self.vars[name] = value

    def as_unreachable(self) -> "Env":
        result = self.copy()
        result.reachable = False
        return result

    def semantic_equal(self, other: "Env") -> bool:
        if self.reachable != other.reachable:
            return False
        keys = set(self.vars) | set(other.vars)
        for key in keys:
            left = self.vars.get(key, UNINIT)
            right = other.vars.get(key, UNINIT)
            if not left.semantic_equal(right):
                return False
        return True

    @staticmethod
    def join(left: "Env", right: "Env", merge_ref: NodeRef | None) -> "Env":
        if not left.reachable:
            return right.copy()
        if not right.reachable:
            return left.copy()
        result = Env(reachable=True)
        for name in set(left.vars) | set(right.vars):
            a = left.vars.get(name, UNINIT)
            b = right.vars.get(name, UNINIT)
            if a.kind == "uninit" and b.kind == "uninit":
                result.vars[name] = UNINIT
            elif a.kind == "uninit" and merge_ref is not None:
                loss = Loss(
                    merge_ref,
                    "alias_unassigned_on_path",
                    f"alias {name!r} is not assigned on every reaching branch",
                )
                result.vars[name] = join_values(unknown_value(loss), b)
            elif b.kind == "uninit" and merge_ref is not None:
                loss = Loss(
                    merge_ref,
                    "alias_unassigned_on_path",
                    f"alias {name!r} is not assigned on every reaching branch",
                )
                result.vars[name] = join_values(a, unknown_value(loss))
            else:
                result.vars[name] = join_values(a, b)
        return result


def join_optional_env(left: Env | None, right: Env | None, merge_ref: NodeRef | None) -> Env | None:
    if left is None:
        return right
    if right is None:
        return left
    return Env.join(left, right, merge_ref)


@dataclass
class Flow:
    env: Env
    returns: PVal = BOTTOM
    break_env: Env | None = None
    continue_env: Env | None = None


@dataclass(frozen=True)
class CtxKey:
    function: str
    args: tuple[PVal, ...]


@dataclass
class CtxState:
    key: CtxKey
    return_value: PVal = BOTTOM
    observations: list[dict[str, Any]] = field(default_factory=list)
    eval_count: int = 0


@dataclass(frozen=True)
class PathEvidence:
    status: str
    code: str
    message: str
    tenant_path: tuple[int, ...] | None
    chain: tuple[NodeRef, ...]
    loss: Loss | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "tenant_path": list(self.tenant_path) if self.tenant_path is not None else None,
            "chain": [node.to_dict() for node in self.chain],
            "loss": self.loss.to_dict() if self.loss else None,
        }


@dataclass
class Judgment:
    status: str  # current | unsafe
    evidence: list[PathEvidence]
    loss: Loss | None = None

    @property
    def safe(self) -> bool:
        return self.status == "current"


def dedup_nodes(nodes: Iterable[NodeRef]) -> tuple[NodeRef, ...]:
    result: list[NodeRef] = []
    seen: set[NodeRef] = set()
    for node in nodes:
        if node not in seen:
            seen.add(node)
            result.append(node)
    return tuple(result)


def static_truth(value: PVal | None) -> bool | None:
    """Return a static condition truth when the restricted DSL makes it known."""

    if value is None or value.kind != "literal":
        return None
    scalar = value.scalar
    if isinstance(scalar, bool):
        return scalar
    if scalar is None:
        return False
    if isinstance(scalar, (int, float, str, bytes)):
        return bool(scalar)
    return None


def earliest_loss(losses: Iterable[Loss | None]) -> Loss | None:
    valid = [loss for loss in losses if loss is not None]
    if not valid:
        return None
    return min(valid, key=lambda loss: (loss.node.order, loss.node.function, loss.node.node_id))


class Analysis:
    def __init__(self, program: Program) -> None:
        self.program = program
        self.contexts: dict[CtxKey, CtxState] = {}
        self.queue: deque[CtxKey] = deque()
        self.queued: set[CtxKey] = set()
        self.dependencies: dict[CtxKey, set[CtxKey]] = defaultdict(set)
        self.reverse_dependencies: dict[CtxKey, set[CtxKey]] = defaultdict(set)
        self.loop_diagnostics: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.sinks = self._collect_sinks()

    # ------------------------------------------------------------------
    # Sink catalogue
    # ------------------------------------------------------------------

    def _collect_sinks(self) -> dict[tuple[str, str], dict[str, Any]]:
        sinks: dict[tuple[str, str], dict[str, Any]] = {}

        def walk(func_name: str, stmts: list[dict[str, Any]]) -> None:
            for stmt in stmts:
                kind = stmt.get("kind") or stmt.get("type")
                if kind in {"cache_read", "read", "cache_write", "write"}:
                    ref = stmt["_ref"]
                    key = (func_name, ref.node_id)
                    sinks[key] = {
                        "key": key,
                        "function": func_name,
                        "node_id": ref.node_id,
                        "ref": ref,
                        "stmt": stmt,
                        "operation": "read" if kind in {"cache_read", "read"} else "write",
                    }
                for child_key in ("then", "else", "body", "statements"):
                    child = stmt.get(child_key)
                    if isinstance(child, list):
                        walk(func_name, child)
                    elif isinstance(child, dict):
                        walk(func_name, [child])
                cases = stmt.get("cases")
                if isinstance(cases, list):
                    for case in cases:
                        if isinstance(case, dict) and isinstance(case.get("body"), list):
                            walk(func_name, case["body"])

        for name, fn in self.program.functions.items():
            walk(name, fn.body)
        return sinks

    # ------------------------------------------------------------------
    # Entry point and fixed point
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        for entry in self.program.entrypoints:
            fn = self.program.require_function(entry["function"])
            raw_args = entry.get("arguments")
            args: list[PVal] = []
            if raw_args is None:
                for param in fn.params:
                    if param.bound_source == "current_tenant":
                        args.append(tenant_value(self.program.current_tenant_id, (param.ref,)))
                    else:
                        loss = Loss(
                            param.ref,
                            "unresolved_entry_parameter",
                            f"entry parameter {param.name!r} has no tenant source or caller argument",
                        )
                        args.append(unknown_value(loss, (param.ref,)))
            else:
                for index, param in enumerate(fn.params):
                    if index < len(raw_args):
                        arg = self.evaluate(raw_args[index], Env(), None, param.ref)
                        arg = with_ref(arg, param.ref)
                    elif param.bound_source == "current_tenant":
                        arg = tenant_value(self.program.current_tenant_id, (param.ref,))
                    else:
                        loss = Loss(
                            param.ref,
                            "missing_argument",
                            f"no argument supplied for parameter {param.name!r}",
                        )
                        arg = unknown_value(loss, (param.ref,))
                    args.append(arg)
                if len(raw_args) > len(fn.params):
                    self.warnings.append(f"entry {fn.name}: extra arguments ignored")
            ctx_key = CtxKey(fn.name, tuple(args))
            self._create_context(ctx_key)

        max_passes = 10000
        passes = 0
        while self.queue:
            ctx_key = self.queue.popleft()
            self.queued.discard(ctx_key)
            state = self.contexts[ctx_key]
            state.eval_count += 1
            passes += 1
            if passes > max_passes:
                raise ProgramValidationError("analysis fixed point did not converge within safety bound")
            self._evaluate_context(ctx_key)

        return self._build_report()

    def _create_context(self, key: CtxKey) -> CtxState:
        if key not in self.contexts:
            self.contexts[key] = CtxState(key)
            self.queue.append(key)
            self.queued.add(key)
        return self.contexts[key]

    def _enqueue(self, key: CtxKey) -> None:
        if key not in self.queued:
            self.queue.append(key)
            self.queued.add(key)

    def _evaluate_context(self, key: CtxKey) -> None:
        state = self.contexts[key]
        state.observations = []
        fn = self.program.require_function(key.function)
        env = Env({param.name: arg for param, arg in zip(fn.params, key.args)}, True)
        flow = self.transfer_statements(fn.body, env, key)
        result = flow.returns
        if flow.env.reachable:
            result = join_values(result, NONE)
        old = state.return_value
        combined = join_values(old, result)
        state.return_value = combined

        if not combined.semantic_equal(old):
            for parent in self.reverse_dependencies.get(key, set()):
                self._enqueue(parent)

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def transfer_statements(
        self,
        stmts: list[dict[str, Any]],
        env: Env,
        ctx: CtxKey | None,
    ) -> Flow:
        current = env.copy()
        returns = BOTTOM
        breaks: Env | None = None
        continues: Env | None = None
        for stmt in stmts:
            if not current.reachable:
                break
            flow = self.transfer_statement(stmt, current, ctx)
            returns = join_values(returns, flow.returns)
            breaks = join_optional_env(breaks, flow.break_env, None)
            continues = join_optional_env(continues, flow.continue_env, None)
            current = flow.env
        return Flow(current, returns, breaks, continues)

    def transfer_statement(self, stmt: dict[str, Any], env: Env, ctx: CtxKey | None) -> Flow:
        kind = stmt.get("kind") or stmt.get("type") or "stmt"
        ref: NodeRef = stmt["_ref"]

        if kind in {"block", "do"}:
            return self.transfer_statements(stmt.get("body") or stmt.get("statements") or [], env, ctx)

        if kind == "assign":
            value = self.evaluate(stmt.get("value", NONE), env, ctx, ref)
            targets = stmt.get("targets")
            if isinstance(targets, list):
                self._bind_targets(targets, value, env, ref)
            else:
                target = stmt.get("target", stmt.get("name"))
                if not isinstance(target, str):
                    raise ProgramValidationError(f"assign {ref.node_id} requires a string target")
                env.set(target, value)
            return Flow(env)

        if kind in {"call", "invoke", "expr"}:
            expr = stmt.get("expr") if kind == "expr" and isinstance(stmt.get("expr"), dict) else stmt
            value = self.evaluate({"kind": "call", **expr} if kind != "expr" else expr, env, ctx, ref)
            target = stmt.get("target") or stmt.get("result")
            if isinstance(target, str):
                env.set(target, value)
            return Flow(env)

        if kind == "return":
            value = self.evaluate(stmt.get("value"), env, ctx, ref) if "value" in stmt else NONE
            return Flow(env.as_unreachable(), with_ref(value, ref))

        if kind in {"break"}:
            return Flow(env.as_unreachable(), break_env=env.copy())

        if kind in {"continue"}:
            return Flow(env.as_unreachable(), continue_env=env.copy())

        if kind in {"raise", "throw"}:
            # Exceptions terminate without a normal or loop exit environment.
            return Flow(env.as_unreachable())

        if kind in {"cache_read", "read", "cache_write", "write"}:
            self.check_sink(stmt, env, ctx)
            target = stmt.get("result") or stmt.get("target")
            if isinstance(target, str):
                loss = Loss(
                    ref,
                    "cache_value_is_not_tenant_source",
                    "value read from cache is data, not a proven current-tenant source when reused as a key",
                )
                env.set(target, unknown_value(loss, (ref,)))
            return Flow(env)

        if kind in {"if", "branch", "when"}:
            return self.transfer_if(stmt, env, ctx, ref)

        if kind in {"while", "loop"}:
            return self.transfer_while(stmt, env, ctx, ref)

        if kind in {"for", "foreach"}:
            return self.transfer_for(stmt, env, ctx, ref)

        if kind in {"switch", "match"}:
            return self.transfer_switch(stmt, env, ctx, ref)

        # Unknown statements are treated as an opaque destructive assignment
        # only if they name a target.  Otherwise they cannot invent key data.
        target = stmt.get("target")
        if isinstance(target, str):
            loss = Loss(ref, "opaque_statement", f"statement kind {kind!r} has no provenance summary")
            env.set(target, unknown_value(loss))
        return Flow(env)

    def _bind_targets(self, targets: list[Any], value: PVal, env: Env, ref: NodeRef) -> None:
        names = [target for target in targets if isinstance(target, str)]
        if value.kind == "tuple" and len(value.items) >= len(names):
            for name, item in zip(names, value.items):
                env.set(name, with_ref(item, ref))
        else:
            loss = Loss(ref, "unpack_provenance_unknown", "could not statically match tuple unpack targets")
            for name in names:
                env.set(name, unknown_value(loss))

    def transfer_if(self, stmt: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef) -> Flow:
        condition_value: PVal | None = None
        if "condition" in stmt or "test" in stmt:
            condition_value = self.evaluate(stmt.get("condition", stmt.get("test")), env, ctx, ref)

        then_body = stmt.get("then") or stmt.get("consequent") or []
        else_body = stmt.get("else", stmt.get("alternate", []))
        truth = static_truth(condition_value)
        if truth is False:
            then_flow = Flow(env.copy().as_unreachable())
        else:
            then_flow = self.transfer_statements(then_body if isinstance(then_body, list) else [then_body], env.copy(), ctx)

        if isinstance(else_body, dict):
            else_body = [else_body]
        if truth is True:
            else_flow = Flow(env.copy().as_unreachable())
        elif else_body:
            else_flow = self.transfer_statements(else_body, env.copy(), ctx)
        else:
            else_flow = Flow(env.copy())

        merged_env = Env.join(then_flow.env, else_flow.env, ref)
        returns = join_values(then_flow.returns, else_flow.returns)
        breaks = join_optional_env(then_flow.break_env, else_flow.break_env, ref)
        continues = join_optional_env(then_flow.continue_env, else_flow.continue_env, ref)
        return Flow(merged_env, returns, breaks, continues)

    def transfer_while(self, stmt: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef) -> Flow:
        body = stmt.get("body") or stmt.get("statements") or []
        condition_expr = stmt.get("condition", stmt.get("test")) if ("condition" in stmt or "test" in stmt) else None

        header = env.copy()
        returns = BOTTOM
        breaks: Env | None = None
        iterations = 0
        converged = False
        max_iterations = 12

        for iterations in range(1, max_iterations + 1):
            if condition_expr is not None:
                condition_value = self.evaluate(condition_expr, header, ctx, ref)
                if static_truth(condition_value) is False:
                    converged = True
                    break

            body_flow = self.transfer_statements(body, header.copy(), ctx)
            returns = join_values(returns, body_flow.returns)
            breaks = join_optional_env(breaks, body_flow.break_env, ref)

            candidate = header
            if body_flow.continue_env is not None:
                candidate = Env.join(candidate, body_flow.continue_env, ref)
            if body_flow.env.reachable:
                candidate = Env.join(candidate, body_flow.env, ref)

            if candidate.semantic_equal(header):
                header = candidate
                converged = True
                break
            header = candidate

        exit_env = Env.join(header, breaks, ref) if breaks is not None else header
        if not converged:
            # Conservative widening for genuinely changing loop states.  State
            # whose semantic lattice already stabilized returned above.
            for name in list(exit_env.vars):
                loss = Loss(ref, "loop_widening_unknown", "loop-carried provenance did not stabilize")
                exit_env.vars[name] = unknown_value(loss)
            self.warnings.append(f"{ref.function}:{ref.node_id}: loop widened to unknown")

        self.loop_diagnostics.append(
            {
                "function": ref.function,
                "node_id": ref.node_id,
                "iterations": iterations,
                "converged": converged,
            }
        )
        # break/continue are consumed by this loop and do not propagate farther.
        return Flow(exit_env, returns)

    def transfer_for(self, stmt: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef) -> Flow:
        variable = stmt.get("variable") or stmt.get("var") or stmt.get("target")
        iterable = self.evaluate(stmt.get("iterable", stmt.get("collection", ())), env, ctx, ref)
        elements: list[PVal] = []

        def collect(value: PVal) -> None:
            if value.kind == "tuple":
                elements.extend(value.items)
            elif value.kind == "alt":
                for item in value.items:
                    collect(item)
            elif value.kind != "bottom":
                loss = Loss(ref, "dynamic_loop_element", "loop elements cannot be enumerated")
                elements.append(unknown_value(loss))

        collect(iterable)
        if isinstance(variable, str):
            env.set(variable, alt_value(elements) if elements else UNINIT)
        return self.transfer_while(stmt, env, ctx, ref)

    def transfer_switch(self, stmt: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef) -> Flow:
        discriminant = stmt.get("value", stmt.get("discriminant"))
        if discriminant is not None:
            self.evaluate(discriminant, env, ctx, ref)
        merged_env: Env | None = None
        returns = BOTTOM
        continues: Env | None = None
        for case in stmt.get("cases", []):
            if not isinstance(case, dict):
                continue
            case_flow = self.transfer_statements(case.get("body", []), env.copy(), ctx)
            returns = join_values(returns, case_flow.returns)
            continues = join_optional_env(continues, case_flow.continue_env, ref)
            case_env = case_flow.env
            if case_flow.break_env is not None:
                # A case-level break terminates only the switch, so its
                # environment is a normal successor after the switch.
                if case_env.reachable:
                    case_env = Env.join(case_env, case_flow.break_env, ref)
                else:
                    case_env = case_flow.break_env
            merged_env = case_env if merged_env is None else Env.join(merged_env, case_env, ref)
        if merged_env is None:
            merged_env = env.copy()
        return Flow(merged_env, returns, None, continues)

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def evaluate(
        self,
        expr: Any,
        env: Env,
        ctx: CtxKey | None,
        ref: NodeRef | None,
    ) -> PVal:
        value = self._evaluate_inner(expr, env, ctx, ref)
        return with_ref(value, ref)

    def _evaluate_inner(
        self,
        expr: Any,
        env: Env,
        ctx: CtxKey | None,
        ref: NodeRef | None,
    ) -> PVal:
        if expr is None:
            return NONE
        if not isinstance(expr, dict):
            return literal_value(expr)

        kind = expr.get("kind") or expr.get("type") or "literal"

        if kind in {"tenant", "current_tenant", "currentTenant"}:
            return tenant_value(self.program.current_tenant_id)

        if kind in {"literal", "const", "scalar"}:
            return literal_value(expr.get("value", expr.get("scalar")))

        if kind in {"tenant_literal"}:
            return literal_value(expr.get("value"), tenant=True)

        if kind in {"var", "variable", "alias", "name"}:
            name = expr.get("name") or expr.get("target")
            if not isinstance(name, str):
                loss = Loss(ref or NodeRef("?", "?"), "bad_variable", "variable expression has no name")
                return unknown_value(loss)
            value = env.get(name)
            if value.kind == "uninit":
                loss = Loss(
                    ref or NodeRef("?", "?"),
                    "undefined_alias",
                    f"alias {name!r} is not defined on this path",
                )
                return unknown_value(loss)
            return value

        if kind in {"tuple", "list", "array"}:
            elements = [self.evaluate(item, env, ctx, ref) for item in expr.get("elements", expr.get("items", []))]
            return tuple_value(elements)

        if kind in {"member", "attr", "attribute", "path"}:
            path = expr.get("path")
            if not path:
                obj = expr.get("object")
                prop = expr.get("property") or expr.get("name")
                if isinstance(obj, str):
                    path = f"{obj}.{prop}"
            if expr.get("tenant_source") is True or path in self.program.tenant_source_paths:
                return tenant_value(self.program.current_tenant_id)
            loss = Loss(ref or NodeRef("?", "?"), "unmodeled_member", f"member {path or '?'} has no provenance summary")
            return unknown_value(loss)

        if kind == "index":
            return self.eval_index(expr, env, ctx, ref)

        if kind == "call":
            return self.eval_call(expr, env, ctx, ref)

        if kind in {"unknown", "input", "request", "attacker"}:
            loss = Loss(
                ref or NodeRef("?", "?"),
                "unknown_external_source",
                expr.get("reason") or "value comes from an unmodeled or attacker-controlled source",
            )
            return unknown_value(loss)

        if kind in {"alt", "coalesce", "either"}:
            return alt_value([self.evaluate(item, env, ctx, ref) for item in expr.get("values", expr.get("args", []))])

        if kind in {"binary", "op", "binop"}:
            return self.eval_binary(expr, env, ctx, ref)

        if kind in {"unary", "not"}:
            operand = self.evaluate(expr.get("value", expr.get("operand")), env, ctx, ref)
            op = expr.get("op") or expr.get("operator") or kind
            if operand.kind == "literal" and isinstance(operand.scalar, bool):
                return literal_value(not operand.scalar if op in {"not", "!"} else operand.scalar)
            return PVal("symbolic_bool")

        # Conservative default: an unrecognized expression cannot prove tenant.
        loss = Loss(
            ref or NodeRef("?", "?"),
            "unmodeled_expression",
            f"expression kind {kind!r} has no provenance summary",
        )
        return unknown_value(loss)

    def eval_index(self, expr: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef | None) -> PVal:
        base = self.evaluate(expr.get("base", expr.get("object")), env, ctx, ref)
        index_value = self.evaluate(expr.get("index", expr.get("key")), env, ctx, ref)

        def index_one(base_value: PVal) -> PVal:
            node = ref or NodeRef("?", "?")
            if base_value.kind == "tuple" and index_value.kind == "literal" and isinstance(index_value.scalar, int):
                idx = index_value.scalar
                if -len(base_value.items) <= idx < len(base_value.items):
                    item = base_value.items[idx]
                    base_safe = judge_value(base_value, node).safe
                    item_safe = judge_value(item, node).safe
                    if base_safe and not item_safe:
                        loss = Loss(
                            node,
                            "tenant_dimension_discarded",
                            f"index operation selects component {idx} and discards the tuple's current-tenant dimension",
                        )
                        return unknown_value(loss, dedup_nodes(tuple(item.trace) + (node,)))
                    return item
            loss = Loss(node, "dynamic_index", "tuple index cannot be resolved to a tenant-preserving component")
            return unknown_value(loss)

        if base.kind == "alt":
            return alt_value([index_one(item) for item in base.items])
        return index_one(base)

    def eval_binary(self, expr: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef | None) -> PVal:
        op = expr.get("op") or expr.get("operator")
        left = self.evaluate(expr.get("left"), env, ctx, ref)
        right = self.evaluate(expr.get("right"), env, ctx, ref)
        if left.kind == "literal" and right.kind == "literal":
            try:
                a, b = left.scalar, right.scalar
                if op in {"+", "add"} and isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    return literal_value(a + b)
                if op in {"-", "sub"} and isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    return literal_value(a - b)
                if op in {"*", "mul"} and isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    return literal_value(a * b)
                if op in {"==", "eq"}:
                    return literal_value(a == b)
                if op in {"!=", "ne"}:
                    return literal_value(a != b)
                if op in {"<", "lt"}:
                    return literal_value(a < b)
                if op in {">", "gt"}:
                    return literal_value(a > b)
                if op in {"<=", "le"}:
                    return literal_value(a <= b)
                if op in {">=", "ge"}:
                    return literal_value(a >= b)
            except TypeError:
                pass
        if op in {"+", "concat", "format", "join"}:
            loss = Loss(
                ref or NodeRef("?", "?"),
                "unstructured_key_combination",
                "string/value concatenation can erase tuple boundaries and is not an unambiguous tenant dimension",
            )
            return unknown_value(loss)
        # A dynamic/non-literal condition has an unknown truth value, so the
        # CFG analysis must retain both successors.
        return PVal("symbolic_bool")

    def eval_call(self, expr: dict[str, Any], env: Env, ctx: CtxKey | None, ref: NodeRef | None) -> PVal:
        callee = expr.get("callee") or expr.get("function") or expr.get("name")
        args = [self.evaluate(arg, env, ctx, ref) for arg in expr.get("args", expr.get("arguments", []))]

        call_ref = ref
        if expr.get("id") and ref is not None:
            call_ref = NodeRef(
                ref.function,
                str(expr["id"]),
                "call",
                f"call {callee}",
                ref.order,
            )

        if not isinstance(callee, str):
            loss = Loss(call_ref or NodeRef("?", "?"), "dynamic_call", "callee is not a static name")
            return unknown_value(loss)

        if callee in self.program.normalizers:
            return self.apply_normalizer(callee, args, call_ref)

        if callee in {"tuple", "list", "array", "make_tuple"}:
            return tuple_value(args)

        if callee in {"alt", "coalesce"}:
            return alt_value(args)

        if callee in self.program.functions:
            return self.invoke_function(callee, args, call_ref, ctx)

        loss = Loss(
            call_ref or NodeRef("?", "?"),
            "external_call_without_summary",
            f"callee {callee!r} is external and no normalizer semantics are declared",
        )
        return unknown_value(loss)

    def apply_normalizer(self, name: str, args: list[PVal], ref: NodeRef | None) -> PVal:
        spec = self.program.normalizers[name]
        mode = str(spec.get("mode") or spec.get("tenant_handling") or "erase").lower()
        value = args[0] if args else BOTTOM
        node = ref or NodeRef("?", "?")

        if mode in {"preserve", "preserve_tenant", "canonicalize_preserving"}:
            allowed_positions = spec.get("tenant_positions") or spec.get("preserve_positions")
            erased_positions = spec.get("erases_positions") or spec.get("erase_positions")
            if allowed_positions is not None or erased_positions is not None:
                judgment = judge_value(value, node)
                if judgment.safe:
                    for evidence in judgment.evidence:
                        path = evidence.tenant_path or ()
                        top = path[0] if path else None
                        if allowed_positions is not None and top not in set(allowed_positions):
                            return self.normalizer_loss(name, value, node, "normalizer_position_erased")
                        if erased_positions is not None and top in set(erased_positions):
                            return self.normalizer_loss(name, value, node, "normalizer_position_erased")
            return with_ref(value, NodeRef(node.function, node.node_id, "normalizer", f"normalizer {name} preserved tenant proof", node.order))

        code = "normalizer_merged_distinctions" if "merge" in mode else "normalizer_erased_tenant"
        return self.normalizer_loss(name, value, node, code)

    def normalizer_loss(self, name: str, value: PVal, node: NodeRef, code: str) -> PVal:
        prior_judgment = judge_value(value, node)
        prior_loss = None if prior_judgment.safe else prior_judgment.loss
        if prior_loss is not None:
            loss = prior_loss
            message = f"normalizer {name!r} further erased tenant distinctions; earliest loss remains {loss.code}"
        else:
            loss = Loss(
                node,
                code,
                f"normalizing/merging function {name!r} removes the distinction that tied this key to the current tenant",
            )
            message = loss.message
        erased_ref = NodeRef(node.function, node.node_id, "normalizer", f"normalizer {name} erased tenant distinction", node.order)
        trace = dedup_nodes(tuple(value.trace) + (erased_ref,))
        result = unknown_value(loss, trace)
        # unknown_value appends loss.node if absent; preserve explicit trace.
        object.__setattr__(result, "trace", dedup_nodes(trace + (loss.node,)))
        return result

    def _nested_loss(self, value: PVal) -> Loss | None:
        if value.loss is not None:
            return value.loss
        losses: list[Loss | None] = []
        if value.kind in {"tuple", "alt"}:
            for item in value.items:
                losses.append(self._nested_loss(item))
        return earliest_loss(losses)

    # ------------------------------------------------------------------
    # Cross-procedure invocation
    # ------------------------------------------------------------------

    def invoke_function(
        self,
        name: str,
        args: list[PVal],
        call_ref: NodeRef | None,
        parent: CtxKey | None,
    ) -> PVal:
        fn = self.program.require_function(name)
        bound: list[PVal] = []
        for index, param in enumerate(fn.params):
            if index < len(args):
                bound.append(with_ref(args[index], param.ref))
            else:
                loss = Loss(param.ref, "missing_argument", f"missing argument for {param.name!r}")
                bound.append(unknown_value(loss, (param.ref,)))
        key = CtxKey(name, tuple(bound))
        state = self._create_context(key)
        if parent is not None:
            self.dependencies[parent].add(key)
            self.reverse_dependencies[key].add(parent)
        if state.return_value.kind == "bottom":
            return BOTTOM
        return with_ref(state.return_value, call_ref)

    # ------------------------------------------------------------------
    # Cache sinks
    # ------------------------------------------------------------------

    def check_sink(self, stmt: dict[str, Any], env: Env, ctx: CtxKey | None) -> None:
        ref: NodeRef = stmt["_ref"]
        key_expr = stmt.get("key", stmt.get("keys"))
        key_value = self.evaluate(key_expr, env, ctx, ref)
        judgment = judge_value(key_value, ref)

        cache_expr = stmt.get("cache", stmt.get("cache_name", stmt.get("cache_expr")))
        cache_value = self.evaluate(cache_expr, env, ctx, ref) if isinstance(cache_expr, (dict, list)) or cache_expr is None else literal_value(cache_expr)
        cache_names = self._literal_names(cache_value)
        if not cache_names:
            cache_names = [None]

        kind = stmt.get("kind") or stmt.get("type") or ""
        if kind in {"cache_read", "read"}:
            operation = "read"
        elif kind in {"cache_write", "write"}:
            operation = "write"
        else:
            operation = str(stmt.get("operation", "access"))

        for cache_name in cache_names:
            spec = self.program.caches.get(cache_name) if cache_name is not None else None
            cache_raw = spec.raw if spec else {}
            allowed_exclusive = False
            exclusive_reason: str | None = None
            if spec is not None:
                allowed_exclusive, exclusive_reason = spec.allows_without_tenant_key(self.program.current_tenant_id)

            if judgment.safe:
                status = "safe"
                reason = "current_tenant_in_key"
                loss = None
            elif allowed_exclusive:
                status = "safe"
                reason = exclusive_reason or "cache_exclusive"
                loss = None
            else:
                status = "unsafe"
                if cache_name is None:
                    reason = "missing_tenant_and_cache_unknown"
                elif spec is None:
                    reason = "missing_tenant_and_cache_not_declared_exclusive"
                else:
                    reason = "missing_tenant_and_cache_not_exclusive_for_current_tenant"
                loss = judgment.loss

            evidence_objects = list(judgment.evidence)
            if not judgment.safe and allowed_exclusive:
                evidence_objects.insert(
                    0,
                    PathEvidence(
                        "cache_exclusive",
                        reason or "cache_exclusive",
                        f"cache {cache_name!r} has an explicit tenancy-exclusive declaration",
                        None,
                        (ref,),
                    ),
                )

            observation = {
                "sink_key": (ref.function, ref.node_id),
                "context": ctx.function if ctx else ref.function,
                "status": status,
                "reason_code": reason,
                "operation": operation,
                "cache": cache_name or "<dynamic>",
                "cache_declared": cache_name in self.program.caches if cache_name else False,
                "cache_exclusive": bool(cache_raw.get("exclusive_tenant") or cache_raw.get("tenant_exclusive") or cache_raw.get("exclusive_to_current_tenant")),
                "key_status": judgment.status,
                "loss": loss.to_dict() if loss else None,
                "evidence": [item.to_dict() for item in evidence_objects],
            }
            if ctx is not None:
                self.contexts[ctx].observations.append(observation)

    def _literal_names(self, value: PVal) -> list[str | None]:
        if value.kind == "literal" and isinstance(value.scalar, str):
            return [value.scalar]
        if value.kind == "alt":
            result: list[str | None] = []
            for item in value.items:
                result.extend(self._literal_names(item))
            return result
        if value.kind == "tuple":
            # A cache name is normally scalar; do not mistake key tuples.
            return []
        return []

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _build_report(self) -> dict[str, Any]:
        all_observations: list[dict[str, Any]] = []
        for state in self.contexts.values():
            all_observations.extend(state.observations)

        sink_reports: list[dict[str, Any]] = []
        for sink_key, info in self.sinks.items():
            observations = [obs for obs in all_observations if tuple(obs["sink_key"]) == sink_key]
            # sink_key tuple survives in memory but JSON conversion later.
            unsafe_obs = [obs for obs in observations if obs["status"] == "unsafe"]
            safe_obs = [obs for obs in observations if obs["status"] == "safe"]

            if not observations:
                status = "unreachable"
                verdict = "unreachable"
                earliest = None
            elif unsafe_obs:
                status = "unsafe"
                verdict = "fail"
                loss_dicts = [obs["loss"] for obs in unsafe_obs if obs.get("loss")]
                earliest = self._earliest_loss_dict(loss_dicts)
            else:
                status = "safe"
                verdict = "pass"
                earliest = None

            reason_source = unsafe_obs if unsafe_obs else safe_obs
            reason_codes: list[str] = []
            for obs in reason_source:
                if obs["reason_code"] not in reason_codes:
                    reason_codes.append(obs["reason_code"])

            report = {
                "id": info["node_id"],
                "node_id": info["node_id"],
                "function": info["function"],
                "operation": info["operation"],
                "status": status,
                "verdict": verdict,
                "reason_code": reason_codes[0] if reason_codes else None,
                "reason_codes": reason_codes,
                "earliest_loss": earliest,
                "first_loss_node_id": earliest["node_id"] if earliest else None,
                "loss_node_id": earliest["node_id"] if earliest else None,
                "context_results": self._dedup_observations(observations),
                "basis": self._select_basis(observations, status),
            }
            sink_reports.append(report)

        order_index = {key: index for index, key in enumerate(self.sinks)}
        sink_reports.sort(key=lambda item: (item["function"], item["node_id"]))

        unsafe_count = sum(1 for item in sink_reports if item["status"] == "unsafe")
        safe_count = sum(1 for item in sink_reports if item["status"] == "safe")
        unreachable_count = sum(1 for item in sink_reports if item["status"] == "unreachable")
        overall = "pass" if unsafe_count == 0 else "fail"
        global_earliest = self._earliest_loss_dict(
            [sink["earliest_loss"] for sink in sink_reports if sink.get("earliest_loss")]
        )
        return {
            "status": overall,
            "ok": unsafe_count == 0,
            "earliest_loss": global_earliest,
            "first_loss_node_id": global_earliest["node_id"] if global_earliest else None,
            "summary": {
                "total": len(sink_reports),
                "safe": safe_count,
                "unsafe": unsafe_count,
                "unreachable": unreachable_count,
                "contexts_analyzed": len(self.contexts),
                "loop_constructs": len(self.loop_diagnostics),
            },
            "sinks": sink_reports,
            "diagnostics": {
                "loops": self.loop_diagnostics,
                "warnings": self.warnings,
            },
            "entrypoints": self.program.entrypoints,
        }

    @staticmethod
    def _earliest_loss_dict(losses: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not losses:
            return None
        return min(losses, key=lambda item: (item.get("location", {}).get("order", 10**12), item.get("function", ""), item.get("node_id", "")))

    @staticmethod
    def _dedup_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for obs in observations:
            key = repr(
                (
                    obs["context"],
                    obs["status"],
                    obs["reason_code"],
                    obs["cache"],
                    obs.get("loss"),
                    tuple(
                        (
                            e["status"],
                            e["code"],
                            tuple(n["node_id"] for n in e["chain"]),
                        )
                        for e in obs.get("evidence", [])
                    ),
                )
            )
            if key not in seen:
                seen.add(key)
                result.append(obs)
        return result

    @staticmethod
    def _select_basis(observations: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
        basis: list[dict[str, Any]] = []
        for obs in observations:
            if status == "safe" and obs["status"] != "safe":
                continue
            if status == "unsafe" and obs["status"] != "unsafe":
                # Include safe path evidence too, making clear that one safe
                # branch does not erase an unsafe reaching branch.
                pass
            for evidence in obs.get("evidence", []):
                chain_ids = tuple(node["node_id"] for node in evidence.get("chain", []))
                item = {
                    "context": obs["context"],
                    "cache": obs["cache"],
                    "observation_status": obs["status"],
                    "reason_code": obs["reason_code"],
                    "evidence_status": evidence["status"],
                    "code": evidence["code"],
                    "message": evidence["message"],
                    "tenant_path": evidence["tenant_path"],
                    "chain": evidence["chain"],
                    "loss": evidence["loss"],
                }
                dedup_key = repr((item["observation_status"], item["evidence_status"], chain_ids, item["loss"]))
                if not any(existing["_key"] == dedup_key for existing in basis):
                    item["_key"] = dedup_key
                    basis.append(item)
        for item in basis:
            item.pop("_key", None)
        return basis


# ---------------------------------------------------------------------------
# Provenance judgment
# ---------------------------------------------------------------------------


def judge_value(value: PVal, sink_ref: NodeRef) -> Judgment:
    if value.kind == "alt":
        child_judgments = [judge_value(item, sink_ref) for item in value.items]
        all_evidence: list[PathEvidence] = []
        losses: list[Loss | None] = []
        all_safe = True
        for child in child_judgments:
            all_evidence.extend(child.evidence)
            losses.append(child.loss)
            if not child.safe:
                all_safe = False
        if all_safe:
            return Judgment("current", all_evidence, None)
        loss = earliest_loss(losses) or _missing_loss(value, sink_ref)
        return Judgment("unsafe", all_evidence, loss)

    if value.kind == "tenant":
        chain = dedup_nodes(value.trace)
        return Judgment(
            "current",
            [
                PathEvidence(
                    "current_tenant",
                    "current_tenant_source",
                    "value is unambiguously derived from the current tenant source",
                    (),
                    chain,
                )
            ],
        )

    if value.kind == "tuple":
        safe_evidence: list[PathEvidence] = []
        unsafe_evidence: list[PathEvidence] = []
        losses: list[Loss | None] = []
        for index, item in enumerate(value.items):
            child = judge_value(item, sink_ref)
            losses.append(child.loss)
            for evidence in child.evidence:
                path = (index,) + (evidence.tenant_path or ())
                chain = dedup_nodes(tuple(evidence.chain) + value.trace)
                extended = PathEvidence(
                    evidence.status,
                    evidence.code,
                    evidence.message,
                    path,
                    chain,
                    evidence.loss,
                )
                if child.safe:
                    safe_evidence.append(extended)
                else:
                    unsafe_evidence.append(extended)
        if safe_evidence:
            return Judgment("current", safe_evidence, None)
        loss = earliest_loss(losses) or _missing_loss(value, sink_ref, "key_has_no_tenant_dimension")
        return Judgment("unsafe", unsafe_evidence, loss)

    if value.kind == "unknown":
        loss = value.loss or _missing_loss(value, sink_ref)
        return Judgment(
            "unsafe",
            [
                PathEvidence(
                    "unknown",
                    loss.code,
                    loss.message,
                    None,
                    dedup_nodes(value.trace),
                    loss,
                )
            ],
            loss,
        )

    if value.kind == "tenant_literal":
        loss = Loss(
            _first_node(value, sink_ref),
            "tenant_literal_not_current_source",
            "literal tenant identity is not tied to the current request tenant",
        )
        return Judgment(
            "unsafe",
            [PathEvidence("other_tenant", loss.code, loss.message, (), dedup_nodes(value.trace), loss)],
            loss,
        )

    code = "missing_tenant_dimension"
    if value.kind == "bottom":
        code = "no_reaching_value"
    elif value.kind == "uninit":
        code = "alias_unassigned_on_path"
    loss = value.loss or _missing_loss(value, sink_ref, code)
    return Judgment(
        "unsafe",
        [PathEvidence("not_tenant", loss.code, loss.message, None, dedup_nodes(value.trace), loss)],
        loss,
    )


def _first_node(value: PVal, sink_ref: NodeRef) -> NodeRef:
    return value.trace[0] if value.trace else sink_ref


def _missing_loss(value: PVal, sink_ref: NodeRef, code: str = "missing_tenant_dimension") -> Loss:
    if value.loss is not None:
        return value.loss
    node = _first_node(value, sink_ref)
    if value.kind == "literal":
        message = "key/path contains only an ordinary literal and no current-tenant dimension"
    elif value.kind == "uninit":
        message = "alias is unassigned on at least one reaching path"
    elif value.kind == "bottom":
        message = "no modeled value reaches this point"
    else:
        message = "key does not contain an unambiguous current-tenant source"
    return Loss(node, code, message)


def analyze(program: Program) -> dict[str, Any]:
    return Analysis(program).run()
