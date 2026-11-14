"""按运行模式的受影响符号可达性分析。

判定规则（对每个模式、每个受影响符号）：
- reachable（已证实可达）：沿该模式适用的 resolved 调用边，从该模式入口
  出发存在到达该符号的路径；输出确定性的最短路径。
- unreachable（在完整证据下不可达）：该模式没有从入口可达的 unresolved
  边（证据完整），且 resolved 图中不存在到达该符号的路径。
- insufficient_evidence（证据不足）：resolved 图中不可达，但存在从入口
  可达的 unresolved 边（动态目标未解析），无法排除其指向受影响符号；
  输出缺少的证据清单。

所有遍历均为确定性 BFS（入口与邻接均排序），循环调用只访问一次，
同一输入重复运行得到一致结果。
"""
from collections import deque

from .model import ALL_MODES, InputError

STATUS_REACHABLE = "reachable"
STATUS_UNREACHABLE = "unreachable"
STATUS_INSUFFICIENT = "insufficient_evidence"

RELEASE_MODE = "release"


def _applicable_edges(dataset, mode):
    return [e for e in dataset.edges
            if e.build_mode == ALL_MODES or e.build_mode == mode]


def _sorted_adjacency(edges):
    adj = {}
    for e in edges:
        adj.setdefault(e.caller, set()).add(e.callee)
    return {caller: sorted(callees) for caller, callees in adj.items()}


def bfs_paths(entries, adj):
    """确定性 BFS，返回 {符号: 从某入口到该符号的最短路径}。

    入口与邻接边均按字典序处理；已访问节点不再入队，循环调用安全。
    """
    paths = {}
    queue = deque()
    for entry in sorted(entries):
        if entry not in paths:
            paths[entry] = [entry]
            queue.append(entry)
    while queue:
        node = queue.popleft()
        for nxt in adj.get(node, ()):
            if nxt not in paths:
                paths[nxt] = paths[node] + [nxt]
                queue.append(nxt)
    return paths


def analyze_mode(dataset, mode):
    edges = _applicable_edges(dataset, mode)
    resolved = [e for e in edges if e.resolution == "resolved"]
    unresolved = sorted((e for e in edges if e.resolution == "unresolved"),
                        key=lambda e: (e.caller, e.callee))
    adj = _sorted_adjacency(resolved)
    entries = dataset.modes[mode]
    paths = bfs_paths(entries, adj)
    reachable = set(paths)

    gaps = [{"caller": e.caller, "callee": e.callee, "reason": e.reason}
            for e in unresolved if e.caller in reachable]

    symbols = {}
    for aff in sorted(dataset.affected, key=lambda a: a.symbol):
        if aff.symbol in reachable:
            symbols[aff.symbol] = {
                "status": STATUS_REACHABLE,
                "path": list(paths[aff.symbol]),
                "missing_evidence": [],
            }
        elif gaps:
            symbols[aff.symbol] = {
                "status": STATUS_INSUFFICIENT,
                "path": [],
                "missing_evidence": [
                    "unresolved edge {caller} -> {callee} ({reason})".format(**g)
                    for g in gaps
                ],
            }
        else:
            symbols[aff.symbol] = {
                "status": STATUS_UNREACHABLE,
                "path": [],
                "missing_evidence": [],
            }

    # 每个入口各自能到达哪些受影响符号（用于发布入口修复优先级）。
    entry_findings = {}
    for entry in sorted(entries):
        entry_paths = bfs_paths([entry], adj)
        hits = {}
        for aff in sorted(dataset.affected, key=lambda a: a.symbol):
            if aff.symbol in entry_paths:
                hits[aff.symbol] = list(entry_paths[aff.symbol])
        if hits:
            entry_findings[entry] = hits

    return {
        "entries": sorted(entries),
        "evidence_complete": not gaps,
        "unresolved_edges_reachable": gaps,
        "symbols": symbols,
        "entry_findings": entry_findings,
    }


def analyze(dataset, modes=None):
    """对选定（或全部）模式执行分析，返回确定性报告字典。"""
    selected = sorted(modes) if modes else sorted(dataset.modes)
    unknown = [m for m in selected if m not in dataset.modes]
    if unknown:
        raise InputError("未知运行模式: " + ", ".join(unknown))

    mode_results = {m: analyze_mode(dataset, m) for m in selected}

    affected_by_symbol = {a.symbol: a for a in dataset.affected}
    priority_fixes = []
    release = mode_results.get(RELEASE_MODE)
    if release:
        for entry in sorted(release["entry_findings"]):
            for symbol in sorted(release["entry_findings"][entry]):
                aff = affected_by_symbol[symbol]
                priority_fixes.append({
                    "mode": RELEASE_MODE,
                    "entry": entry,
                    "symbol": symbol,
                    "advisory": aff.advisory,
                    "severity": aff.severity,
                    "path": release["entry_findings"][entry][symbol],
                })

    return {
        "schema": 1,
        "tool": "sec-vuln-004-reachability",
        "instances": [
            {"id": i.id, "name": i.name, "version": i.version}
            for i in sorted(dataset.instances, key=lambda i: i.id)
        ],
        "affected": [
            {"symbol": a.symbol, "advisory": a.advisory, "severity": a.severity}
            for a in sorted(dataset.affected, key=lambda a: a.symbol)
        ],
        "modes": mode_results,
        "priority_fixes": priority_fixes,
    }
