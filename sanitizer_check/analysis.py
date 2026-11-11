"""Fixpoint data-flow engine.

Per field we track a *set* of :class:`PathState` values, one element per
distinct reaching path.  Confluence (branch merge) is set union, so a field
that is sanitized on one branch and tainted on another keeps both states.
The whole domain is finite -- each PathState has a boolean tainted/unknown
flag and a subset of the finite sanitizer vocabulary -- therefore every
worklist iteration terminates.

Interprocedural analysis uses function summaries memoized by
(entry environment, function).  Recursion is handled by re-running callers
with the latest summary until the summary map reaches a fixpoint.
"""

from __future__ import annotations

from .errors import AnalysisError
from .model import (
    Env,
    Graph,
    Node,
    PathState,
    empty_env,
    join_envs,
)

# Safety valve for the worklist.  The true bound is the size of the finite
# state lattice (see TERMINATION in the README); this constant only guards
# against implementation bugs.
_MAX_WORKLIST_STEPS = 1_000_000
_MAX_SUMMARY_ROUNDS = 10_000


def _env_key(env: Env):
    return tuple(sorted(
        (f, frozenset(states)) for f, states in env.items()
    ))


class Engine:
    def __init__(self, graph: Graph):
        self.graph = graph
        # node id -> environment holding at the entry of the node
        self.node_envs: dict = {}
        # (function id, entry-env key) -> (entry_env, exit_env, ret_env)
        self._summaries: dict = {}
        self._sanitizer_vocab = self._collect_sanitizers()

    # ------------------------------------------------------------------ utils
    def _collect_sanitizers(self) -> frozenset:
        vocab = set()
        for n in self.graph.nodes.values():
            vocab.update(n.sanitizers)
        return frozenset(vocab)

    def _put(self, node_id: str, env: Env) -> bool:
        """Join env into node's holding environment; report whether it grew."""
        before = self.node_envs.get(node_id)
        if before is None:
            merged = {f: frozenset(s) for f, s in env.items()}
        else:
            merged = join_envs(before, env)
        if before != merged:
            self.node_envs[node_id] = merged
            return True
        return False

    # ------------------------------------------------------------- transfer
    def _transfer(self, node: Node, env: Env) -> Env:
        out = {f: frozenset(states) for f, states in env.items()}

        if node.kind == "source":
            states = set(out.get(node.field, frozenset()))
            states.add(PathState(
                tainted=True,
                unknown=False,
                sanitizers=(),
                origins=(node.source_label or node.id,),
            ))
            out[node.field] = frozenset(states)

        elif node.kind == "stmt":
            if node.field is not None and node.sanitizers:
                states = out.get(node.field, frozenset())
                folded = set(states)
                for san in node.sanitizers:
                    folded = {ps.with_sanitizer(san) for ps in folded}
                # A sanitizer applied to a field with no reaching value still
                # records nothing; sanitization without taint is irrelevant.
                if folded:
                    out[node.field] = frozenset(folded)

        elif node.kind == "unmodeled":
            # The graph gives up at this point: every live field may contain
            # data of unknown origin.  A named field with no predecessor value
            # itself becomes an unknown origin (e.g. an opaque API response).
            if out:
                for f, states in list(out.items()):
                    converted = {
                        PathState(
                            tainted=True,
                            unknown=True,
                            sanitizers=ps.sanitizers,
                            origins=ps.origins,
                        )
                        for ps in states
                    }
                    out[f] = frozenset(converted)
            if node.field is not None:
                out[node.field] = out.get(node.field, frozenset()) | \
                    frozenset({PathState(tainted=True, unknown=True,
                                         origins=("UNMODELED:" + node.id,))})

        elif node.kind == "call":
            out = self._transfer_call(node, out)

        # entry, return, merge, branch, sink: identity transfer.
        return out

    def _transfer_call(self, node: Node, env: Env) -> Env:
        out = {f: frozenset(s) for f, s in env.items()}
        callee = self.graph.functions.get(node.callee) if node.callee else None

        if callee is None or node.id in self.graph.incompleteness:
            # Unresolved callee / missing call edge: return value is tainted
            # with an unknown origin.  Actual arguments themselves keep their
            # current states (may-analysis: the callee might not touch them).
            if node.ret_field is not None:
                contributed = set()
                for _formal, actual in node.bindings:
                    for ps in env.get(actual, frozenset()):
                        contributed.add(ps.as_unknown_return())
                if not contributed:
                    contributed.add(PathState(tainted=True, unknown=True))
                out[node.ret_field] = frozenset(contributed)
            return out

        # Build the callee entry environment from positional bindings.
        callee_entry: Env = {}
        for formal, actual in node.bindings:
            callee_entry[formal] = frozenset(env.get(actual, frozenset()))

        _entry_env, _exit_env, ret_env = self._analyze_function(
            node.callee, callee_entry)

        # Only the declared return field flows back to the caller.
        if node.ret_field is not None:
            returned = ret_env.get("__return__", frozenset())
            out[node.ret_field] = frozenset(returned) if returned else frozenset()
        return out

    # ------------------------------------------------------- function summary
    def _analyze_function(self, func_id: str, entry_env: Env):
        key = (func_id, _env_key(entry_env))
        cached = self._summaries.get(key)
        if cached is not None:
            return cached

        # Seed a bottom summary so a recursive call terminates immediately;
        # rounds below widen it until the summary map reaches a fixpoint.
        bottom = (entry_env, empty_env(), empty_env())
        self._summaries[key] = bottom

        rounds = 0
        while True:
            rounds += 1
            if rounds > _MAX_SUMMARY_ROUNDS:
                raise AnalysisError(
                    f"summary fixpoint did not converge for {func_id}")
            exit_env, ret_env = self._run_worklist(func_id, entry_env)
            current = (entry_env, exit_env, ret_env)
            previous = self._summaries[key]
            self._summaries[key] = current
            if previous[1] == exit_env and previous[2] == ret_env:
                return current

    def _run_worklist(self, func_id: str, entry_env: Env):
        func = self.graph.functions[func_id]
        own_nodes = {
            nid for nid, n in self.graph.nodes.items()
            if n.function == func_id
        }
        # Seed the entry node with this particular invocation's environment.
        self._put(func.entry, entry_env)
        worklist = [func.entry]
        queued = {func.entry}
        steps = 0

        while worklist:
            steps += 1
            if steps > _MAX_WORKLIST_STEPS:
                raise AnalysisError(
                    f"worklist exceeded {_MAX_WORKLIST_STEPS} steps in "
                    f"{func_id}")
            nid = worklist.pop()
            queued.discard(nid)
            node = self.graph.nodes[nid]
            holding = self.node_envs.get(nid, empty_env())
            out_env = self._transfer(node, holding)
            for succ in self.graph.successors(nid):
                if succ not in own_nodes:
                    continue  # interprocedural edges are handled via summaries
                if self._put(succ, out_env) and succ not in queued:
                    worklist.append(succ)
                    queued.add(succ)

        exit_env = empty_env()
        for rid in func.returns:
            exit_env = join_envs(exit_env,
                                 self.node_envs.get(rid, empty_env()))
        ret_env = self._extract_return(func_id, exit_env)
        return exit_env, ret_env

    def _extract_return(self, func_id: str, exit_env: Env) -> Env:
        """Collect values visible at return statements into __return__.

        A return node may name a field; if none does, every field leaving the
        function is considered a possible return (loose graph style).
        """
        return_nodes = [
            self.graph.nodes[rid] for rid in
            self.graph.functions[func_id].returns
        ]
        named = [n.field for n in return_nodes if n.field is not None]
        if named:
            merged = frozenset()
            for f in named:
                merged = merged | frozenset(exit_env.get(f, frozenset()))
            return {"__return__": merged}
        merged = frozenset()
        for states in exit_env.values():
            merged = merged | frozenset(states)
        return {"__return__": merged}

    # ----------------------------------------------------------------- entry
    def _root_functions(self, declared_roots):
        if declared_roots:
            unknown = [r for r in declared_roots if r not in self.graph.functions]
            if unknown:
                raise AnalysisError(f"unknown root function(s): {unknown}")
            return list(declared_roots)
        callees = {
            n.callee for n in self.graph.nodes.values() if n.kind == "call"
        }
        roots = [fid for fid in self.graph.functions if fid not in callees]
        return roots or list(self.graph.functions)

    def analyze(self, declared_roots=()):
        """Run the analysis from every root function in the graph."""
        for func_id in self._root_functions(tuple(declared_roots)):
            self._analyze_function(func_id, empty_env())
        return self.node_envs
