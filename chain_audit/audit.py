"""链路核查主审计器。

用法::

    auditor = ChainAuditor()
    auditor.ingest(IntakeDoc(...), MeasurementDoc(...))
    auditor.node_interval("S1")
    auditor.sources_of("T1")
    auditor.contradiction

每次 ingest 都基于全部已收单据重新编译求解：证据迟到时新约束只会
收窄合法区间（或暴露矛盾），不存在“先到先得”的顺序依赖。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from .compiler import ANON, Model, compile_documents
from .intervals import (
    Equation,
    clear_caches,
    extremize,
    propagate,
    ratio_extremize,
    solve,
)
from .model import (
    BlindGenerationDoc,
    ContradictionReport,
    DisclosedLinkDoc,
    IngestReport,
    InputError,
    IntakeDoc,
    MeasurementDoc,
    MixDecl,
    NodeInfo,
    SourceAttribution,
    SourceShare,
    SplitDecl,
)

SOLVE_BUDGET = 500_000
SEARCH_BUDGET = 300_000


class ChainAuditor:
    def __init__(self) -> None:
        self._docs: list = []
        self._model: Optional[Model] = None
        self._domains: Optional[dict[str, tuple[int, int]]] = None
        self._status = "empty"  # empty | satisfiable | unsatisfiable | unknown
        self._contradiction: Optional[ContradictionReport] = None
        self._witness: Optional[dict[str, int]] = None
        self._version = 0
        self._g_cache: dict[int, dict[tuple[str, ...], str]] = {}

    # ------------------------------------------------------------------
    # 增量收窄
    # ------------------------------------------------------------------
    def ingest(self, *documents) -> IngestReport:
        for doc in documents:
            if any(d.doc_id == doc.doc_id for d in self._docs):
                raise InputError(f"单据编号重复: {doc.doc_id}")
            self._docs.append(doc)
        self._version += 1
        self._rebuild()
        return IngestReport(
            version=self._version,
            status=self._status,
            contradiction=self._contradiction,
            doc_count=len(self._docs),
        )

    def _rebuild(self) -> None:
        self._g_cache = {}
        clear_caches()
        model = compile_documents(self._docs)
        self._model = model
        prop = propagate(model.domains, model.equations)
        if prop.failed is not None:
            self._domains = prop.domains
            self._status = "unsatisfiable"
            self._contradiction = self._compute_mus(model, prop.failed)
            self._witness = None
            return
        status, witness = solve(
            prop.domains, model.equations, node_budget=SOLVE_BUDGET
        )
        self._domains = prop.domains
        self._witness = witness
        if status == "sat":
            self._status = "satisfiable"
            self._contradiction = None
        elif status == "unsat":
            self._status = "unsatisfiable"
            self._contradiction = self._compute_mus(model, None)
        else:
            self._status = "unknown"
            self._contradiction = None

    # ------------------------------------------------------------------
    # 矛盾定位：删除法求不可满足单据子集（MUS）
    # ------------------------------------------------------------------
    def _compute_mus(
        self, base_model: Model, failed_var: Optional[str]
    ) -> ContradictionReport:
        all_ids = list(base_model.order)

        def subset_status(ids) -> str:
            sub = [base_model.docs[i] for i in ids]
            m = compile_documents(sub)
            p = propagate(m.domains, m.equations)
            if p.failed is not None:
                return "unsat"
            status, _ = solve(p.domains, m.equations, node_budget=SOLVE_BUDGET)
            return status

        core = list(all_ids)
        fully_verified = True
        for doc_id in all_ids:
            trial = [i for i in core if i != doc_id]
            if not trial:
                continue
            result = subset_status(trial)
            if result == "unsat":
                core = trial  # 该单据对矛盾不必要
            elif result == "unknown":
                fully_verified = False
        # 复核：core 自身必须仍不可满足。
        if subset_status(core) != "unsat":  # pragma: no cover - 防御
            fully_verified = False
        reasons = [self._describe(doc_id, base_model) for doc_id in core]
        return ContradictionReport(
            doc_ids=tuple(core),
            reasons=tuple(reasons),
            failed_variable=failed_var,
            minimal=fully_verified,
        )

    def _describe(self, doc_id: str, model: Model) -> str:
        doc = model.docs[doc_id]
        if isinstance(doc, IntakeDoc):
            return (
                f"入库单 {doc_id}: {doc.batch}/{doc.node_id} "
                f"总量 [{doc.amount_lo}, {doc.amount_hi}] mg"
            )
        if isinstance(doc, MeasurementDoc):
            return (
                f"测量单 {doc_id}: {doc.batch}/{doc.node_id} "
                f"测得 [{doc.amount_lo}, {doc.amount_hi}] mg"
            )
        if isinstance(doc, SplitDecl):
            return (
                f"拆分声明 {doc_id}: {doc.parent_id} -> "
                f"{', '.join(doc.child_ids)}"
            )
        if isinstance(doc, MixDecl):
            return (
                f"混样声明 {doc_id}: {', '.join(doc.parent_ids)} -> "
                f"{doc.child_id}"
            )
        if isinstance(doc, DisclosedLinkDoc):
            rng = (
                f" [{doc.amount_lo}, {doc.amount_hi}] mg"
                if doc.amount_lo is not None
                else ""
            )
            return (
                f"分拣单披露 {doc_id}: {doc.parent_id} -> "
                f"{doc.child_id}{rng}"
            )
        if isinstance(doc, BlindGenerationDoc):
            return (
                f"盲号世代 {doc_id}: {doc.batch}/{doc.blind_id}"
                f"#gen{doc.generation} = {doc.node_id}"
            )
        return f"单据 {doc_id}"  # pragma: no cover

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def status(self) -> str:
        return self._status

    @property
    def contradiction(self) -> Optional[ContradictionReport]:
        return self._contradiction

    @property
    def documents(self) -> tuple:
        return tuple(self._docs)

    def doc(self, doc_id: str):
        for doc in self._docs:
            if doc.doc_id == doc_id:
                return doc
        raise KeyError(doc_id)

    def node_info(self, node_id: str) -> NodeInfo:
        self._require_model(node_id)
        return self._model.nodes[node_id]

    def resolve_blind(
        self, batch: str, blind_id: str, generation: int
    ) -> Optional[str]:
        if self._model is None:
            return None
        return self._model.labels.get((batch, blind_id, generation))

    def node_interval(self, node_id: str) -> Optional[tuple[int, int]]:
        """传播收窄后的节点毫克闭区间；矛盾状态下返回 None。"""
        self._require_model(node_id)
        if self._status == "unsatisfiable":
            return None
        name = self._model.node_var[node_id]
        return self._domains[name]

    def feasible_witness(self) -> Optional[dict[str, int]]:
        """一份满足全部约束的整数毫克赋值（内部变量名 -> 值）。"""
        if self._witness is None:
            return None
        return dict(self._witness)

    def candidate_sources(self, node_id: str) -> tuple[str, ...]:
        """所有可能贡献该节点的来源（含未披露的匿名来源，若其量可正）。"""
        return tuple(s.source_id for s in self.sources_of(node_id).shares)

    def sources_of(self, node_id: str) -> SourceAttribution:
        model = self._require_model(node_id)
        exact_overall = self._status != "unknown"
        nvar = model.node_var[node_id]
        nlo, nhi = self._domains[nvar]

        # 原样节点：唯一来源就是自己，比例恒为 1。
        if node_id in model.sources:
            return SourceAttribution(
                node_id=node_id,
                amount_lo=nlo,
                amount_hi=nhi,
                shares=(
                    SourceShare(
                        source_id=node_id,
                        amount_lo=nlo,
                        amount_hi=nhi,
                        fraction_lo=Fraction(1),
                        fraction_hi=Fraction(1),
                        exact=True,
                    ),
                ),
                unique=True,
            )

        terms = self._source_inflow_terms(model, node_id)
        shares: list[SourceShare] = []
        for source_id, term_vars in terms:
            amount_lo, amount_hi, amount_exact = self._amount_bounds(
                model, term_vars
            )
            if amount_hi == 0:
                continue  # 该来源不可能贡献，剔除
            frac_lo = frac_hi = None
            frac_exact = amount_exact
            if nhi > 0:
                g_name = self._ensure_g(model, term_vars)
                frac_lo, ok_lo = ratio_extremize(
                    self._domains,
                    model.equations,
                    g_name,
                    nvar,
                    maximize=False,
                    node_budget=SEARCH_BUDGET,
                )
                frac_hi, ok_hi = ratio_extremize(
                    self._domains,
                    model.equations,
                    g_name,
                    nvar,
                    maximize=True,
                    node_budget=SEARCH_BUDGET,
                )
                frac_exact = amount_exact and ok_lo and ok_hi
            shares.append(
                SourceShare(
                    source_id=source_id,
                    amount_lo=amount_lo,
                    amount_hi=amount_hi,
                    fraction_lo=frac_lo,
                    fraction_hi=frac_hi,
                    exact=exact_overall and frac_exact,
                )
            )
        unique = len(shares) == 1 and shares[0].source_id != ANON
        return SourceAttribution(
            node_id=node_id,
            amount_lo=nlo,
            amount_hi=nhi,
            shares=tuple(shares),
            unique=unique,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _require_model(self, node_id: str) -> Model:
        if self._model is None:
            raise InputError("尚未收到任何单据")
        if node_id not in self._model.nodes:
            raise InputError(f"未知节点: {node_id}")
        return self._model

    def _source_inflow_terms(
        self, model: Model, node_id: str
    ) -> list[tuple[str, list[str]]]:
        """返回 [(source, 构成该来源在 node 处流入量的变量列表)]，含匿名项。"""
        incoming = [e for e in model.edges if e.child == node_id]
        terms: list[tuple[str, list[str]]] = []
        source_set = set(model.in_sources.get(node_id, set()))
        # 入边上实际承载的来源（封闭节点只可能通过这些路径得到料）。
        for e in incoming:
            source_set.update(model.reach[e.parent])
        for s in sorted(source_set):
            vars_for_s = [
                model.f_var[(e.parent, e.child, s)]
                for e in incoming
                if s in model.reach[e.parent]
            ]
            if node_id in model.virtual_var and (node_id, s) in model.vf_var:
                vars_for_s.append(model.vf_var[(node_id, s)])
            if vars_for_s:
                terms.append((s, vars_for_s))
        anon_vars: list[str] = []
        if node_id in model.virtual_var and node_id in model.anon_virtual_var:
            anon_vars.append(model.anon_virtual_var[node_id])
        if anon_vars:
            terms.append((ANON, anon_vars))
        return terms

    def _amount_bounds(
        self, model: Model, term_vars: list[str]
    ) -> tuple[int, int, bool]:
        if len(term_vars) == 1:
            lo, hi = self._domains[term_vars[0]]
            return lo, hi, True
        g_name = self._ensure_g(model, term_vars)
        lo, hi = self._domains[g_name]
        min_v, ok_min = extremize(
            self._domains,
            model.equations,
            g_name,
            maximize=False,
            node_budget=SEARCH_BUDGET,
        )
        max_v, ok_max = extremize(
            self._domains,
            model.equations,
            g_name,
            maximize=True,
            node_budget=SEARCH_BUDGET,
        )
        exact = ok_min and ok_max and min_v is not None and max_v is not None
        return (
            min_v if min_v is not None else lo,
            max_v if max_v is not None else hi,
            exact,
        )

    def _ensure_g(self, model: Model, term_vars: list[str]) -> str:
        """为一个线性和临时加一个 g 变量与 g=sum(terms) 方程。"""
        key = tuple(sorted(term_vars))
        cache = self._g_cache.setdefault(id(model), {})
        if key in cache:
            return cache[key]
        name = f"g_{len(cache)}"
        cache[key] = name
        # g 是 term_vars 之和，其上界取各项上界之和即充分且紧凑。
        cap = sum(model.domains[v][1] for v in term_vars)
        model.domains[name] = (0, cap)
        self._domains[name] = (0, cap)
        model.var_docs[name] = []
        coeffs = [(name, -1)] + [(v, 1) for v in term_vars]
        model.equations.append(Equation(tuple(coeffs), 0))
        # 立即对新方程做一次传播，保持 self._domains 最新。
        prop = propagate(self._domains, model.equations)
        if prop.failed is None:
            self._domains = prop.domains
        return name


__all__ = ["ChainAuditor", "SOLVE_BUDGET", "SEARCH_BUDGET"]
