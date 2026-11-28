"""把可查单据编译为整数区间约束模型。

模型分两层：

* 标量层：每个节点一个毫克量变量，每条声明边一个流量变量，
  非原样节点额外有一个“未声明来源”虚拟流量变量，满足入=出守恒；
* 多商品流层：每个入库原样是一种商品，外加一个匿名商品
  （承接分拣单未披露的来源），边上的商品分量之和等于该边总流量。

未声明来源之所以必须建模：分拣单只披露部分父子关系，模块不能把
“没看到”当成“不存在”，匿名商品保证所有合法来源区间都被保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .intervals import Equation
from .model import (
    GLOBAL_CAP,
    BlindGenerationDoc,
    DisclosedLinkDoc,
    InputError,
    IntakeDoc,
    MeasurementDoc,
    MixDecl,
    NodeInfo,
    SplitDecl,
)

ANON = "@unattributed"


@dataclass
class Edge:
    parent: str
    child: str
    batch: str
    origins: tuple[str, ...]


@dataclass
class Model:
    docs: dict[str, object]
    order: tuple[str, ...]
    nodes: dict[str, NodeInfo]
    edges: list[Edge]
    sources: list[str]
    terminals: list[str]
    reach: dict[str, list[str]]  # 节点 -> 可到达它的真实原样来源
    in_sources: dict[str, set[str]]  # 非来源节点 -> 入边/虚拟边带来的来源并集
    node_var: dict[str, str]
    edge_var: dict[tuple[str, str], str]
    f_var: dict[tuple[str, str, str], str]  # (parent, child, source) -> 变量
    anon_edge_var: dict[tuple[str, str], str]
    virtual_var: dict[str, str]
    vf_var: dict[tuple[str, str], str]  # (node, source) -> 虚拟边商品分量
    anon_virtual_var: dict[str, str]
    terminal_share_var: dict[tuple[str, str], str]  # (node, source)
    anon_terminal_var: dict[str, str]
    domains: dict[str, tuple[int, int]]
    equations: list[Equation]
    labels: dict[tuple[str, str, int], str]  # 盲号 (batch, blind, gen) -> node

    # ---- 编译期登记的变量->单据来源，用于矛盾定位 ----
    var_docs: dict[str, list[str]] = field(default_factory=dict)

    def edge_of(self, parent: str, child: str) -> Optional[Edge]:
        for e in self.edges:
            if e.parent == parent and e.child == child:
                return e
        return None


def compile_documents(documents) -> Model:
    docs: dict[str, object] = {}
    order: list[str] = []
    for doc in documents:
        if doc.doc_id in docs:
            raise InputError(f"单据编号重复: {doc.doc_id}")
        docs[doc.doc_id] = doc
        order.append(doc.doc_id)

    node_batch: dict[str, str] = {}
    node_flags: dict[str, set[str]] = {}
    node_bounds: dict[str, list[tuple[int, int, str]]] = {}
    edge_map: dict[tuple[str, str], Edge] = {}
    edge_bounds: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
    # 被拆分/混样声明完整覆盖的子节点：其父子关系集合视为完整，
    # 不允许额外的未声明流入；分拣单披露不产生这种封闭性。
    closed_children: set[str] = set()
    # 封闭父节点：拆分的父、混样的子，其入=出严格守恒，不允许留样松弛。
    closed_parents: set[str] = set()
    labels: dict[tuple[str, str, int], str] = {}
    sources: list[str] = []

    def register(node_id: str, batch: str, flag: str) -> None:
        if not node_id:
            raise InputError(f"批次 {batch} 出现空节点编号")
        old = node_batch.get(node_id)
        if old is not None and old != batch:
            raise InputError(
                f"节点 {node_id} 同时出现在批次 {old} 与 {batch}，跨批节点必须拆开编号"
            )
        node_batch[node_id] = batch
        node_flags.setdefault(node_id, set()).add(flag)

    def add_edge(parent: str, child: str, batch: str, doc_id: str) -> None:
        key = (parent, child)
        if key in edge_map:
            edge = edge_map[key]
            if edge.batch != batch:
                raise InputError(f"边 {parent}->{child} 跨批次冲突")
            edge_map[key] = Edge(
                edge.parent,
                edge.child,
                edge.batch,
                tuple(sorted(set(edge.origins) | {doc_id})),
            )
        else:
            edge_map[key] = Edge(parent, child, batch, (doc_id,))

    for doc_id in order:
        doc = docs[doc_id]
        if isinstance(doc, IntakeDoc):
            register(doc.node_id, doc.batch, "source")
            if doc.node_id not in sources:
                sources.append(doc.node_id)
            node_bounds.setdefault(doc.node_id, []).append(
                (doc.amount_lo, doc.amount_hi, doc_id)
            )
        elif isinstance(doc, SplitDecl):
            register(doc.parent_id, doc.batch, "split")
            for child in doc.child_ids:
                register(child, doc.batch, "derived")
                add_edge(doc.parent_id, child, doc.batch, doc_id)
                closed_children.add(child)
            closed_parents.add(doc.parent_id)
        elif isinstance(doc, MixDecl):
            register(doc.child_id, doc.batch, "mix")
            for parent in doc.parent_ids:
                register(parent, doc.batch, "derived")
                add_edge(parent, doc.child_id, doc.batch, doc_id)
            closed_children.add(doc.child_id)
            closed_parents.add(doc.child_id)
        elif isinstance(doc, MeasurementDoc):
            register(doc.node_id, doc.batch, "measured")
            node_bounds.setdefault(doc.node_id, []).append(
                (doc.amount_lo, doc.amount_hi, doc_id)
            )
        elif isinstance(doc, DisclosedLinkDoc):
            register(doc.parent_id, doc.batch, "derived")
            register(doc.child_id, doc.batch, "derived")
            add_edge(doc.parent_id, doc.child_id, doc.batch, doc_id)
            if doc.amount_lo is not None:
                edge_bounds.setdefault((doc.parent_id, doc.child_id), []).append(
                    (doc.amount_lo, doc.amount_hi, doc_id)
                )
        elif isinstance(doc, BlindGenerationDoc):
            key = (doc.batch, doc.blind_id, doc.generation)
            if key in labels and labels[key] != doc.node_id:
                raise InputError(
                    f"盲号 {doc.blind_id} 世代 {doc.generation} 在批次 {doc.batch} "
                    f"绑定冲突: {labels[key]} vs {doc.node_id}"
                )
            labels[key] = doc.node_id
        else:  # pragma: no cover - 防御
            raise InputError(f"未知单据类型: {type(doc)!r}")

    # 盲号绑定的节点：引用的节点先登记（盲号可能先于保管单据到达），
    # 批次不一致仍是结构错误；冲突在上面的 labels 检查中处理。
    for (batch, blind, gen), node_id in labels.items():
        old = node_batch.get(node_id)
        if old is not None and old != batch:
            raise InputError(
                f"盲号 {blind} 世代 {gen} 跨批绑定节点 {node_id}"
            )
        if old is None:
            register(node_id, batch, "blind-ref")

    edges = list(edge_map.values())
    _ensure_dag(edges)

    # 模型局部上限：所有入库/测量单据上界之和。任何守恒流量都不可能
    # 超过已观测物料总量，用它替代全局 cap 可大幅缩小搜索域。
    model_cap = 0
    for doc in docs.values():
        if isinstance(doc, (IntakeDoc, MeasurementDoc)):
            model_cap += doc.amount_hi
    if model_cap <= 0:
        model_cap = GLOBAL_CAP

    # 节点类型（来源优先，其次 split/mix，其余 derived/measured）。
    nodes: dict[str, NodeInfo] = {}
    outgoing = {n: 0 for n in node_batch}
    for edge in edges:
        outgoing[edge.parent] += 1
    for node_id, batch in node_batch.items():
        flags = node_flags[node_id]
        if "source" in flags:
            kind = "source"
        elif "split" in flags:
            kind = "split"
        elif "mix" in flags:
            kind = "mix"
        elif "measured" in flags:
            kind = "measured"
        else:
            kind = "derived"
        lo = hi = None
        for blo, bhi, _ in node_bounds.get(node_id, ()):
            lo = blo if lo is None else max(lo, blo)
            hi = bhi if hi is None else min(hi, bhi)
        if lo is None:
            lo, hi = 0, model_cap
        nodes[node_id] = NodeInfo(
            node_id=node_id,
            batch=batch,
            kind=kind,
            amount_lo=lo,
            amount_hi=hi,
            is_terminal=outgoing[node_id] == 0,
        )

    # 可达来源（拓扑序上做集合传播）。
    topo = _topo_order(edges, list(node_batch))
    reach_set: dict[str, set[str]] = {n: set() for n in node_batch}
    incoming: dict[str, list[Edge]] = {n: [] for n in node_batch}
    for edge in edges:
        incoming[edge.child].append(edge)
    for node_id in topo:
        if node_id in sources:
            reach_set[node_id].add(node_id)
        for edge in incoming[node_id]:
            reach_set[node_id].update(reach_set[edge.parent])
    reach = {n: sorted(s) for n, s in reach_set.items()}
    terminals = [n for n in topo if outgoing[n] == 0 and n not in sources]

    model = Model(
        docs=docs,
        order=tuple(order),
        nodes=nodes,
        edges=edges,
        sources=sources,
        terminals=terminals,
        reach=reach,
        in_sources={},
        node_var={},
        edge_var={},
        f_var={},
        anon_edge_var={},
        virtual_var={},
        vf_var={},
        anon_virtual_var={},
        terminal_share_var={},
        anon_terminal_var={},
        domains={},
        equations=[],
        labels=labels,
    )

    var_docs: dict[str, list[str]] = {}
    domains: dict[str, tuple[int, int]] = {}

    def add_var(name: str, lo: int, hi: int, origins) -> None:
        if name in domains:  # pragma: no cover - 命名唯一，防御
            raise InputError(f"内部变量重名: {name}")
        domains[name] = (lo, hi)
        var_docs[name] = list(origins)

    # 节点量变量（区间来自入库单/测量单的交集）。
    for node_id, info in nodes.items():
        name = f"n_{len(model.node_var)}"
        model.node_var[node_id] = name
        add_var(
            name,
            info.amount_lo,
            info.amount_hi,
            [d for _, _, d in node_bounds.get(node_id, ())],
        )

    # 边流量变量（区间来自披露分拣单的交集；无披露则全区间）。
    for i, edge in enumerate(edges):
        name = f"x_{i}"
        model.edge_var[(edge.parent, edge.child)] = name
        lo, hi, origins = 0, model_cap, list(edge.origins)
        for blo, bhi, doc_id in edge_bounds.get((edge.parent, edge.child), ()):
            lo, hi = max(lo, blo), min(hi, bhi)
            origins.append(doc_id)
        add_var(name, lo, hi, origins)

    # 标量层虚拟“未声明来源”流量：
    #   * 拆分/混样声明的子节点是封闭的，父子关系集合完整，无虚拟流入；
    #   * 只被分拣单部分披露/无入边的节点保留虚拟流入，承接未披露来源。
    for node_id in topo:
        if node_id in sources or node_id in closed_children:
            continue
        name = f"v_{len(model.virtual_var)}"
        model.virtual_var[node_id] = name
        add_var(name, 0, model_cap, [])

    # 多商品流：声明边只承载真实来源商品；匿名（未披露）来源只通过
    # 虚拟边进入节点，不在任何已声明边上流动。
    for i, edge in enumerate(edges):
        for s in reach[edge.parent]:
            fname = f"f_{i}_{sources.index(s)}"
            model.f_var[(edge.parent, edge.child, s)] = fname
            add_var(fname, 0, model_cap, [])

    # 每个非来源节点能接触到的来源：入边父节点可达来源的并集，
    # 再并上自身（无入边时）可达来源。
    in_sources: dict[str, set[str]] = {}
    for node_id in topo:
        if node_id in sources:
            continue
        union = set(reach[node_id])
        for edge in incoming[node_id]:
            union.update(reach[edge.parent])
        in_sources[node_id] = union
    model.in_sources = in_sources

    # 商品层虚拟变量：仅为真正拥有标量虚拟边的节点创建。
    # 封闭子节点（拆分/混样声明覆盖）没有未声明流入，不建这些变量，
    # 避免出现不受任何方程约束的孤立商品变量。
    for node_id in topo:
        if node_id not in model.virtual_var:
            continue
        for s in sorted(in_sources[node_id]):
            wname = f"w_{len(model.vf_var)}_{sources.index(s)}"
            model.vf_var[(node_id, s)] = wname
            add_var(wname, 0, model_cap, [])
        w0 = f"w0_{len(model.anon_virtual_var)}"
        model.anon_virtual_var[node_id] = w0
        add_var(w0, 0, model_cap, [])

    for node_id in terminals:
        if node_id not in model.virtual_var:
            # 封闭终端：各来源/匿名分量已由入边平衡方程确定，无需终端变量。
            continue
        for s in in_sources[node_id]:
            aname = f"a_{len(model.terminal_share_var)}_{sources.index(s)}"
            model.terminal_share_var[(node_id, s)] = aname
            add_var(aname, 0, model_cap, [])
        a0 = f"a0_{len(model.anon_terminal_var)}"
        model.anon_terminal_var[node_id] = a0
        add_var(a0, 0, model_cap, [])

    equations: list[Equation] = []

    # 源节点的未声明去向（留样/未披露分流）松弛变量：标量 + 每商品。
    # 被拆分/混样声明覆盖的封闭父节点严格守恒，不允许留样松弛。
    source_slack: dict[str, str] = {}
    source_slack_f: dict[tuple[str, str], str] = {}
    for node_id in sources:
        if node_id in closed_parents:
            continue
        out_edges = [e for e in edges if e.parent == node_id]
        if not out_edges:
            continue
        sname = f"k_{len(source_slack)}"
        source_slack[node_id] = sname
        add_var(sname, 0, model_cap, [])
        for s in reach[node_id]:
            sf = f"kf_{len(source_slack_f)}_{sources.index(s)}"
            source_slack_f[(node_id, s)] = sf
            add_var(sf, 0, model_cap, [])

    # ---- 标量守恒 ----
    for node_id in topo:
        nvar = model.node_var[node_id]
        out_edges = [e for e in edges if e.parent == node_id]
        in_edges = incoming[node_id]
        if node_id in sources:
            if out_edges:
                coeffs = [
                    (model.edge_var[(e.parent, e.child)], 1)
                    for e in out_edges
                ]
                if node_id in source_slack:
                    # 开放源：声明流出 + 未声明去向 = 入库量。
                    coeffs.append((source_slack[node_id], 1))
                # 封闭源（被拆分/混样声明覆盖）：流出之和严格等于入库量。
                coeffs.append((nvar, -1))
                equations.append(Equation(tuple(coeffs), 0))
            continue
        coeffs = []
        if node_id in model.virtual_var:
            coeffs.append((model.virtual_var[node_id], 1))
        coeffs += [
            (model.edge_var[(e.parent, e.child)], 1) for e in in_edges
        ]
        if out_edges:
            coeffs += [
                (model.edge_var[(e.parent, e.child)], -1) for e in out_edges
            ]
        else:
            coeffs.append((nvar, -1))
        if coeffs:
            equations.append(Equation(tuple(coeffs), 0))

    # ---- 边的商品分解：声明边流量等于各真实来源分量之和 ----
    for edge in edges:
        key = (edge.parent, edge.child)
        coeffs = [
            (model.f_var[(edge.parent, edge.child, s)], 1)
            for s in reach[edge.parent]
        ]
        coeffs.append((model.edge_var[key], -1))
        equations.append(Equation(tuple(coeffs), 0))

    # ---- 源未声明去向的商品分解 ----
    for node_id, sname in source_slack.items():
        coeffs = [
            (source_slack_f[(node_id, s)], 1) for s in reach[node_id]
        ]
        coeffs.append((sname, -1))
        equations.append(Equation(tuple(coeffs), 0))

    # ---- 虚拟边的商品分解 ----
    for node_id in topo:
        if node_id not in model.virtual_var:
            continue
        coeffs = [(model.anon_virtual_var[node_id], 1)]
        coeffs += [
            (model.vf_var[(node_id, s)], 1) for s in sorted(in_sources[node_id])
        ]
        coeffs.append((model.virtual_var[node_id], -1))
        equations.append(Equation(tuple(coeffs), 0))

    # ---- 逐商品节点平衡 ----
    for s in sources:
        for node_id in topo:
            if node_id == s:
                # 原样节点自身商品：声明流出 + 未声明去向 = 节点量。
                out_edges = [e for e in edges if e.parent == node_id]
                if not out_edges:
                    continue
                coeffs = [
                    (model.f_var[(e.parent, e.child, s)], 1) for e in out_edges
                ]
                if (node_id, s) in source_slack_f:
                    coeffs.append((source_slack_f[(node_id, s)], 1))
                coeffs.append((model.node_var[node_id], -1))
                equations.append(Equation(tuple(coeffs), 0))
                continue
            if s not in in_sources.get(node_id, set()):
                continue
            out_edges = [
                e
                for e in edges
                if e.parent == node_id and s in reach_set[e.parent]
            ]
            in_edges = [
                e
                for e in incoming[node_id]
                if s in reach_set[e.parent]
            ]
            coeffs = []
            if node_id in model.virtual_var and (node_id, s) in model.vf_var:
                vfvar = model.vf_var[(node_id, s)]
                coeffs.append((vfvar, 1))
            coeffs += [
                (model.f_var[(e.parent, e.child, s)], 1) for e in in_edges
            ]
            if out_edges:
                coeffs += [
                    (model.f_var[(e.parent, e.child, s)], -1)
                    for e in out_edges
                ]
            elif (node_id, s) in model.terminal_share_var:
                coeffs.append(
                    (model.terminal_share_var[(node_id, s)], -1)
                )
            else:
                # 封闭终端：商品守恒已由边分解与标量终端平衡保证，跳过。
                continue
            equations.append(Equation(tuple(coeffs), 0))

    # ---- 匿名商品平衡：匿名来源只经虚拟边进入，且不能跨声明边转发 ----
    # 因此开放节点的匿名流入 w0 必须等于其终端匿名分量 a0。
    for node_id in topo:
        if node_id in sources:
            continue
        if node_id not in model.anon_virtual_var:
            continue
        out_edges = [e for e in edges if e.parent == node_id]
        coeffs = [(model.anon_virtual_var[node_id], 1)]
        if not out_edges and node_id in model.anon_terminal_var:
            coeffs.append((model.anon_terminal_var[node_id], -1))
        elif out_edges:
            # 有出边时匿名料无法沿声明边流动：虚拟匿名流入必须为 0。
            pass
        equations.append(Equation(tuple(coeffs), 0))

    # ---- 终端：各商品分量之和等于节点测得总量（仅开放终端有分量变量）----
    for node_id in terminals:
        if node_id not in model.anon_terminal_var:
            continue
        coeffs = [(model.anon_terminal_var[node_id], 1)]
        coeffs += [
            (model.terminal_share_var[(node_id, s)], 1)
            for s in sorted(in_sources[node_id])
        ]
        coeffs.append((model.node_var[node_id], -1))
        equations.append(Equation(tuple(coeffs), 0))

    model.domains = domains
    model.equations = equations
    model.var_docs = var_docs
    return model


def _ensure_dag(edges: list[Edge]) -> None:
    adj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge.parent, []).append(edge.child)
        adj.setdefault(edge.child, [])
    color: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in adj.get(node, ()):
            if color.get(nxt) == 1:
                cycle = " -> ".join(stack[stack.index(nxt):] + [nxt])
                raise InputError(f"保管关系图存在环路: {cycle}")
            if color.get(nxt) != 2:
                visit(nxt, stack)
        stack.pop()
        color[node] = 2

    for node in adj:
        if color.get(node) != 2:
            visit(node, [])


def _topo_order(edges: list[Edge], nodes: list[str]) -> list[str]:
    indeg = {n: 0 for n in nodes}
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for edge in edges:
        adj[edge.parent].append(edge.child)
        indeg[edge.child] += 1
    queue = sorted(n for n in nodes if indeg[n] == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(adj[node]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(nodes):  # pragma: no cover - 已先做 DAG 校验
        raise InputError("保管关系图存在环路")
    return order


__all__ = ["ANON", "Edge", "Model", "compile_documents"]
