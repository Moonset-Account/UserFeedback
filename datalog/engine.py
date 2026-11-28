"""核心引擎：规则表示、分层、逐层朴素求不动点、结果解释与事实更正。

术语
----
- EDB：外延数据库关系，即外部输入的事实；
- IDB：内涵数据库关系，即由规则推导出的关系；
- 层（stratum）：分层后每个谓词所在的非负整数层；否定只能引用
  严格更低层的谓词，因此被否定的检查在求值本层时其目标层已封闭；
- 朴素求值（naive evaluation）：每层反复应用该层的全部规则直到
  关系集合不再变化。关系是有限集合，重复推导同一行不会重复存储，
  因此每轮不增加新行时不动点必然到达，计算不会无限继续。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class Var:
    """逻辑变量。规则项中凡是 ``Var`` 实例即变量，其余值均为常量。"""

    __slots__ = ("name",)

    def __init__(self, name: str):
        if not isinstance(name, str) or not name:
            raise TypeError("变量名必须是非空字符串")
        self.name = name

    def __repr__(self) -> str:
        return f"Var({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Var) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("Var", self.name))


class Atom:
    """原子，形如 ``edge(X, Y)``。

    ``terms`` 中可出现 :class:`Var` 或任意可哈希常量。
    """

    __slots__ = ("predicate", "terms")

    def __init__(self, predicate: str, *terms: Any):
        if not isinstance(predicate, str) or not predicate:
            raise TypeError("谓词名必须是非空字符串")
        self.predicate = predicate
        self.terms = tuple(terms)

    def __repr__(self) -> str:
        args = ", ".join(_format_term(t) for t in self.terms)
        return f"{self.predicate}({args})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Atom)
            and self.predicate == other.predicate
            and self.terms == other.terms
        )

    def __hash__(self) -> int:
        return hash((self.predicate, self.terms))


class Literal:
    """规则体文字。``negated=True`` 表示取反（否定即失败）。"""

    __slots__ = ("atom", "negated")

    def __init__(self, atom: Atom, negated: bool = False):
        if not isinstance(atom, Atom):
            raise TypeError("Literal 必须基于 Atom 构造")
        self.atom = atom
        self.negated = negated

    def __repr__(self) -> str:
        prefix = "NOT " if self.negated else ""
        return f"{prefix}{self.atom!r}"


def NOT(atom: Atom) -> Literal:
    """构造被否定的文字，例如 ``NOT(edge(X, Y))``。"""
    return Literal(atom, negated=True)


class Rule:
    """一条规则 ``head :- body...``，``location`` 用于在错误中标注规则位置。"""

    __slots__ = ("head", "body", "location")

    def __init__(self, head: Atom, body: list[Literal], location: Optional[str] = None):
        if not isinstance(head, Atom):
            raise TypeError("规则头必须是 Atom")
        body = tuple(body)
        if not body:
            raise ValueError("规则体不能为空（本引擎不接受无条件推导规则）")
        for lit in body:
            if not isinstance(lit, Literal):
                raise TypeError("规则体元素必须是 Literal，可用 NOT(atom) 取反")
        self.head = head
        self.body = body
        self.location = location

    def __repr__(self) -> str:
        loc = f" @ {self.location}" if self.location is not None else ""
        return f"{self.head!r} :- {', '.join(repr(b) for b in self.body)}{loc}"


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class DatalogError(Exception):
    """引擎相关错误的基类。"""


class SafetyError(DatalogError):
    """规则不满足范围限制（安全条件）或谓词元数不一致。"""


class StratificationError(DatalogError):
    """含否定依赖的环导致程序无法分层。

    属性
    ----
    cycle:
        环上的谓词序列（首尾谓词相同），例如 ``["p", "q", "p"]``。
    rule_locations:
        环上每条依赖边对应的规则位置（无显式位置时使用规则索引字符串）。
    negative_edges:
        环上的否定依赖，元素为 ``(源谓词, 被否定谓词)``。
    """

    def __init__(
        self,
        message: str,
        cycle: list[str],
        rule_locations: list[str],
        negative_edges: list[tuple[str, str]],
    ):
        super().__init__(message)
        self.cycle = cycle
        self.rule_locations = rule_locations
        self.negative_edges = negative_edges


# ---------------------------------------------------------------------------
# 解释结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NegationCheck:
    """一次否定（缺席）检查的记录。

    ``stratum`` 是被否定关系求值时所在、且在检查时已经封闭的层。
    """

    predicate: str
    tuple_value: tuple
    stratum: int
    absent: bool = True

    def __str__(self) -> str:
        return (
            f"NOT {self.predicate}({', '.join(map(repr, self.tuple_value))}) "
            f"[缺席检查：第 {self.stratum} 层已封闭，缺席={str(self.absent).lower()}]"
        )


@dataclass(frozen=True)
class ProofNode:
    """一行输出的一条有限正向推导树。

    - 对 EDB 事实：``rule=None``、``children=()``、``stratum=0``；
    - 对 IDB 推导：``rule`` 为产生该行的规则，``children`` 按规则体
      顺序排列；正向文字给出被连接行的子证明，否定文字给出
      :class:`NegationCheck`；
    - ``stratum`` 是结论关系所在的层。
    """

    predicate: str
    tuple_value: tuple
    stratum: int
    rule: Optional[Rule]
    children: tuple = field(default_factory=tuple)

    def is_fact(self) -> bool:
        return self.rule is None

    def negation_checks(self) -> list[NegationCheck]:
        """收集整棵证明树中出现的全部否定检查。"""
        found: list[NegationCheck] = []
        for child in self.children:
            if isinstance(child, NegationCheck):
                found.append(child)
            elif isinstance(child, ProofNode):
                found.extend(child.negation_checks())
        return found


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class QueryEngine:
    """对一组有限关系与声明式规则做分层不动点求值。

    典型用法::

        engine = QueryEngine()
        engine.add_fact("edge", ("a", "b"))
        engine.add_rule(Rule(Atom("reach", Var("X"), Var("Y")),
                             [Literal(Atom("edge", Var("X"), Var("Y")))]),
                        location="r1")
        engine.evaluate()
        engine.rows("reach")
        engine.explain("reach", ("a", "b"))
    """

    def __init__(self) -> None:
        self._rules: list[Rule] = []
        self._facts: dict[str, set[tuple]] = defaultdict(set)
        self._relations: dict[str, set[tuple]] = {}
        self._proofs: dict[tuple, ProofNode] = {}
        self._pred_strata: dict[str, int] = {}
        self._rule_strata: dict[int, int] = {}
        self._strata_report: dict[int, dict[str, Any]] = {}
        self._evaluated = False

    # ----- 程序装载 ------------------------------------------------------

    def add_rule(self, rule: Rule, location: Optional[str] = None) -> None:
        """加入规则。``location`` 可覆盖规则自带位置，缺省用规则序号标注。"""
        if not isinstance(rule, Rule):
            raise TypeError("add_rule 需要 Rule 实例")
        idx = len(self._rules)
        effective_location = (
            location if location is not None
            else (rule.location if rule.location is not None else f"rule#{idx}")
        )
        if rule.location is None and location is None:
            rule = Rule(rule.head, list(rule.body), location=effective_location)
        elif location is not None:
            rule = Rule(rule.head, list(rule.body), location=effective_location)
        self._rules.append(rule)
        self._evaluated = False

    def add_fact(self, predicate: str, tuple_value: tuple) -> None:
        """加入（或更正补入）一个基础事实。重复事实自动去重。"""
        tuple_value = tuple(tuple_value)
        self._facts[predicate].add(tuple_value)
        self._evaluated = False

    def retract_fact(self, predicate: str, tuple_value: tuple) -> bool:
        """撤去一个基础事实，返回它原先是否存在。撤去后需重新 :meth:`evaluate`。"""
        tuple_value = tuple(tuple_value)
        existed = tuple_value in self._facts.get(predicate, set())
        self._facts[predicate].discard(tuple_value)
        self._evaluated = False
        return existed

    def replace_facts(self, predicate: str, tuples: list[tuple]) -> None:
        """整体更正某个基础关系的事实集合。"""
        self._facts[predicate] = {tuple(t) for t in tuples}
        self._evaluated = False

    # ----- 静态检查 ------------------------------------------------------

    def _collect_predicates(self) -> tuple[dict[str, int], dict[str, int]]:
        arity: dict[str, int] = {}

        def record(atom: Atom) -> None:
            if atom.predicate in arity and arity[atom.predicate] != len(atom.terms):
                raise SafetyError(
                    f"谓词 {atom.predicate!r} 的元数不一致："
                    f"已有 {arity[atom.predicate]}，又出现 {len(atom.terms)}"
                )
            arity[atom.predicate] = len(atom.terms)

        for predicate, tuples in self._facts.items():
            for tuple_value in tuples:
                if predicate in arity and arity[predicate] != len(tuple_value):
                    raise SafetyError(
                        f"基础关系 {predicate!r} 的元数不一致：期望 "
                        f"{arity[predicate]}，事实 {tuple_value!r} 为 {len(tuple_value)}"
                    )
                arity[predicate] = len(tuple_value)
        for rule in self._rules:
            record(rule.head)
            for lit in rule.body:
                record(lit.atom)
        pred_arity = dict(arity)
        return pred_arity, {i: self._rules[i].head.predicate for i in range(len(self._rules))}

    def _check_safety(self) -> None:
        for rule in self._rules:
            pos_vars: set[Var] = set()
            for lit in rule.body:
                if not lit.negated:
                    pos_vars.update(v for v in lit.atom.terms if isinstance(v, Var))
            neg_vars: set[Var] = set()
            for lit in rule.body:
                if lit.negated:
                    neg_vars.update(v for v in lit.atom.terms if isinstance(v, Var))
            unbound = sorted(v.name for v in neg_vars - pos_vars)
            if unbound:
                raise SafetyError(
                    f"规则 {rule.location!r} 中否定文字的变量 {unbound} "
                    "未被同一规则体的正向文字绑定（不满足范围限制）"
                )
            head_vars = {v for v in rule.head.terms if isinstance(v, Var)}
            unbound_head = sorted(v.name for v in head_vars - pos_vars)
            if unbound_head:
                raise SafetyError(
                    f"规则 {rule.location!r} 的头变量 {unbound_head} "
                    "未被规则体正向文字绑定（不满足范围限制）"
                )
            head_constants = [t for t in rule.head.terms if not isinstance(t, Var)]
            for const in head_constants:
                if not _hashable(const):
                    raise SafetyError(
                        f"规则 {rule.location!r} 的规则头含不可哈希常量 {const!r}"
                    )

    # ----- 分层 ----------------------------------------------------------

    def _stratify(self) -> None:
        # 依赖边：head_pred -> body_pred；否定边单独标记。
        edges: dict[str, set[str]] = defaultdict(set)
        neg_edge_rules: dict[tuple[str, str], list[str]] = defaultdict(list)
        preds: set[str] = set(self._facts.keys())
        for rule in self._rules:
            hp = rule.head.predicate
            preds.add(hp)
            for lit in rule.body:
                bp = lit.atom.predicate
                preds.add(bp)
                edges[hp].add(bp)
                if lit.negated:
                    neg_edge_rules[(hp, bp)].append(
                        rule.location if rule.location is not None else "?"
                    )

        # Tarjan 强连通分量（迭代写法，避免深递归程序触发递归上限）。
        index_counter = [0]
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        sccs: list[list[str]] = []

        def strongconnect(root: str) -> None:
            work: list[tuple[str, int]] = [(root, 0)]
            indices[root] = lowlink[root] = index_counter[0]
            index_counter[0] += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                v, next_i = work[-1]
                neighbors = sorted(edges.get(v, ()))
                if next_i < len(neighbors):
                    w = neighbors[next_i]
                    work[-1] = (v, next_i + 1)
                    if w not in indices:
                        indices[w] = lowlink[w] = index_counter[0]
                        index_counter[0] += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, 0))
                    elif w in on_stack:
                        lowlink[v] = min(lowlink[v], indices[w])
                else:
                    if lowlink[v] == indices[v]:
                        component: list[str] = []
                        while True:
                            w = stack.pop()
                            on_stack.discard(w)
                            component.append(w)
                            if w == v:
                                break
                        sccs.append(component)
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        lowlink[parent] = min(lowlink[parent], lowlink[v])

        for pred in sorted(preds):
            if pred not in indices:
                strongconnect(pred)

        scc_of: dict[str, int] = {}
        for scc_id, component in enumerate(sccs):
            for pred in component:
                scc_of[pred] = scc_id

        # 在 SCC 缩点图上找一条经过否定边的有向环，连同规则位置返回。
        neg_pairs = set(neg_edge_rules.keys())
        for src, dst in sorted(neg_pairs):
            if scc_of[src] != scc_of[dst]:
                continue
            if src == dst:
                # 自否定：零长度路径即闭合成环 [src, src]。
                found = True
                forward = [src]
            else:
                # BFS 求 dst -> src 的分量内路径，与否定边 src -> dst 闭合成环。
                came_from: dict[str, Optional[str]] = {dst: None}
                queue = deque([dst])
                found = False
                while queue and not found:
                    cur = queue.popleft()
                    for nxt in sorted(edges.get(cur, ())):
                        if scc_of.get(nxt) != scc_of[src] or nxt in came_from:
                            continue
                        came_from[nxt] = cur
                        if nxt == src:
                            found = True
                            break
                        queue.append(nxt)
                if not found:
                    continue
                # 由 BFS 父指针还原 dst -> ... -> src 的分量内正向路径。
                forward = []
                node: Optional[str] = src
                while node is not None:
                    forward.append(node)
                    node = came_from[node]
                forward.reverse()  # dst, ..., src
            # 完整环：src -(否定)-> dst -> ... -> src。
            cycle = [src] + forward
            cycle_edges = list(zip(cycle[:-1], cycle[1:]))
            locations: list[str] = []
            negative_on_cycle: list[tuple[str, str]] = []
            for a, b in cycle_edges:
                if (a, b) in neg_pairs:
                    negative_on_cycle.append((a, b))
                    locations.append(",".join(neg_edge_rules[(a, b)]))
                else:
                    locations.append(self._edge_rule_location(a, b))
            raise StratificationError(
                "检测到含否定依赖的环，程序无法分层："
                + " -> ".join(cycle)
                + f"；其中否定边 {negative_on_cycle}",
                cycle=cycle,
                rule_locations=locations,
                negative_edges=negative_on_cycle,
            )

        # 缩点 DAG 上以最长路径编号分层：层号 = 最长依赖链长度。
        cond_edges: dict[int, set[int]] = defaultdict(set)
        for u, neighbors in edges.items():
            for v in neighbors:
                cu, cv = scc_of[u], scc_of[v]
                if cu != cv and cv not in cond_edges[cu]:
                    cond_edges[cu].add(cv)
        # 依赖方向 head -> body，层号应满足 stratum(head) >= stratum(body)，
        # 否定时严格大于。按 body -> head 的拓扑方向计算最长链。
        reverse: dict[int, set[int]] = defaultdict(set)
        rev_indegree: dict[int, int] = {i: 0 for i in range(len(sccs))}
        for cu, neighbors in cond_edges.items():
            for cv in neighbors:
                reverse[cv].add(cu)
                rev_indegree[cu] += 1
        scc_stratum = {i: 0 for i in range(len(sccs))}
        ready = deque(sorted(i for i, d in rev_indegree.items() if d == 0))
        while ready:
            cid = ready.popleft()
            for parent in sorted(reverse.get(cid, ())):
                edge_is_negative = any(
                    (u, v) in neg_pairs
                    for u in sccs[parent]
                    for v in sccs[cid]
                )
                candidate = scc_stratum[cid] + (1 if edge_is_negative else 0)
                if candidate > scc_stratum[parent]:
                    scc_stratum[parent] = candidate
                rev_indegree[parent] -= 1
                if rev_indegree[parent] == 0:
                    ready.append(parent)

        self._pred_strata = {pred: scc_stratum[scc_of[pred]] for pred in preds}
        self._rule_strata = {
            i: self._pred_strata[rule.head.predicate]
            for i, rule in enumerate(self._rules)
        }

        report: dict[int, dict[str, Any]] = {}
        for pred, level in self._pred_strata.items():
            report.setdefault(level, {"predicates": set(), "rule_locations": []})
            report[level]["predicates"].add(pred)
        for i, rule in enumerate(self._rules):
            level = self._rule_strata[i]
            loc = rule.location if rule.location is not None else f"rule#{i}"
            report[level]["rule_locations"].append(loc)
        self._strata_report = {
            level: {
                "predicates": sorted(info["predicates"]),
                "rule_locations": list(info["rule_locations"]),
            }
            for level, info in sorted(report.items())
        }

    def _edge_rule_location(self, src: str, dst: str) -> str:
        for rule in self._rules:
            if rule.head.predicate != src:
                continue
            if any(lit.atom.predicate == dst for lit in rule.body):
                return rule.location if rule.location is not None else "?"
        return "?"

    # ----- 求值 ----------------------------------------------------------

    def evaluate(self) -> "QueryEngine":
        """执行静态检查、分层与逐层不动点求值，返回引擎自身以便链式调用。"""
        self._check_safety()
        self._collect_predicates()
        self._stratify()

        relations: dict[str, set[tuple]] = {}
        proofs: dict[tuple, ProofNode] = {}
        all_preds = set(self._pred_strata)
        for pred in all_preds:
            relations[pred] = set()

        # EDB 事实位于第 0 层，作为封闭的基础关系注入。
        for pred, tuples in self._facts.items():
            for tuple_value in tuples:
                relations[pred].add(tuple_value)
                proofs[(pred, tuple_value)] = ProofNode(
                    predicate=pred,
                    tuple_value=tuple_value,
                    stratum=0,
                    rule=None,
                    children=(),
                )

        max_stratum = max(self._pred_strata.values(), default=0)
        rules_by_stratum: dict[int, list[int]] = defaultdict(list)
        for rule_index, level in self._rule_strata.items():
            rules_by_stratum[level].append(rule_index)

        for level in range(max_stratum + 1):
            rule_indices = rules_by_stratum.get(level, [])
            if not rule_indices:
                continue
            # 朴素不动点：反复应用本层全部规则，直到没有任何新行。
            # 行存于有限集合，重复推导被去重，故无新行时必然终止。
            changed = True
            while changed:
                changed = False
                for rule_index in rule_indices:
                    rule = self._rules[rule_index]
                    for substitutions, child_proofs in self._eval_body(
                        rule, relations, proofs, level
                    ):
                        derived = tuple(
                            substitutions[t] if isinstance(t, Var) else t
                            for t in rule.head.terms
                        )
                        target = relations[rule.head.predicate]
                        if derived not in target:
                            target.add(derived)
                            proofs[(rule.head.predicate, derived)] = ProofNode(
                                predicate=rule.head.predicate,
                                tuple_value=derived,
                                stratum=level,
                                rule=rule,
                                children=tuple(child_proofs),
                            )
                            changed = True

        self._relations = relations
        self._proofs = proofs
        self._evaluated = True
        return self

    def _match_atom(
        self,
        atom: Atom,
        substitution: dict[Var, Any],
        relations: dict[str, set[tuple]],
    ) -> list[tuple[dict[Var, Any], tuple]]:
        """对单个正向原子在当前关系上做连接，返回（新替换, 命中行）列表。"""
        matches: list[tuple[dict[Var, Any], tuple]] = []
        for row in relations.get(atom.predicate, set()):
            if len(row) != len(atom.terms):
                continue
            new_sub = dict(substitution)
            ok = True
            for term, value in zip(atom.terms, row):
                if isinstance(term, Var):
                    if term in new_sub:
                        if new_sub[term] != value:
                            ok = False
                            break
                    else:
                        new_sub[term] = value
                else:
                    if term != value:
                        ok = False
                        break
            if ok:
                matches.append((new_sub, row))
        return matches

    def _eval_body(
        self,
        rule: Rule,
        relations: dict[str, set[tuple]],
        proofs: dict[tuple, ProofNode],
        current_stratum: int,
    ) -> list[tuple[dict[Var, Any], list[Any]]]:
        results: list[tuple[dict[Var, Any], list[Any]]] = []

        def walk(
            index: int,
            substitution: dict[Var, Any],
            children: list[Any],
        ) -> None:
            if index == len(rule.body):
                results.append((substitution, children))
                return
            lit = rule.body[index]
            atom = lit.atom
            if not lit.negated:
                for new_sub, row in self._match_atom(atom, substitution, relations):
                    child_proof = proofs.get((atom.predicate, row))
                    assert child_proof is not None
                    walk(index + 1, new_sub, children + [child_proof])
                return

            # 否定文字：其变量必须已被正向文字绑定（安全条件已保证）。
            grounded = tuple(
                substitution[t] if isinstance(t, Var) else t for t in atom.terms
            )
            if grounded not in relations.get(atom.predicate, set()):
                check = NegationCheck(
                    predicate=atom.predicate,
                    tuple_value=grounded,
                    stratum=self._pred_strata[atom.predicate],
                    absent=True,
                )
                walk(index + 1, substitution, children + [check])

        walk(0, {}, [])
        return results

    # ----- 结果访问与解释 ------------------------------------------------

    def _require_evaluated(self) -> None:
        if not self._evaluated:
            raise DatalogError("引擎尚未求值，请先调用 evaluate()")

    def rows(self, predicate: str) -> list[tuple]:
        """返回某关系的全部行，按稳定顺序排列。"""
        self._require_evaluated()
        rows_set = self._relations.get(predicate, set())
        return sorted(rows_set, key=lambda t: repr(t))

    def contains(self, predicate: str, tuple_value: tuple) -> bool:
        self._require_evaluated()
        return tuple(tuple_value) in self._relations.get(predicate, set())

    def explain(self, predicate: str, tuple_value: tuple) -> ProofNode:
        """展开输出行的一条有限正向推导树；找不到时抛出 ``KeyError``。"""
        self._require_evaluated()
        tuple_value = tuple(tuple_value)
        proof = self._proofs.get((predicate, tuple_value))
        if proof is None:
            raise KeyError(f"关系 {predicate!r} 中不存在行 {tuple_value!r}，无推导可解释")
        return proof

    def format_proof(self, proof: ProofNode) -> str:
        """将 :class:`ProofNode` 渲染为可读的缩进推导树。"""
        lines: list[str] = []

        def render(node: ProofNode, depth: int) -> None:
            indent = "  " * depth
            header = (
                f"{node.predicate}({', '.join(map(repr, node.tuple_value))})"
                f" [第 {node.stratum} 层]"
            )
            if node.is_fact():
                lines.append(f"{indent}{header} :- 基础事实")
                return
            loc = node.rule.location if node.rule is not None else "?"
            lines.append(f"{indent}{header} :- 规则 {loc}")
            for child in node.children:
                if isinstance(child, NegationCheck):
                    lines.append(f"{'  ' * (depth + 1)}{child}")
                else:
                    render(child, depth + 1)

        render(proof, 0)
        return "\n".join(lines)

    def strata(self) -> dict[int, dict[str, Any]]:
        """返回分层报告：每层包含哪些谓词与规则位置。"""
        self._require_evaluated()
        return {
            level: {
                "predicates": list(info["predicates"]),
                "rule_locations": list(info["rule_locations"]),
            }
            for level, info in self._strata_report.items()
        }

    def predicate_stratum(self, predicate: str) -> int:
        self._require_evaluated()
        return self._pred_strata[predicate]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _format_term(term: Any) -> str:
    if isinstance(term, Var):
        return term.name
    return repr(term)
