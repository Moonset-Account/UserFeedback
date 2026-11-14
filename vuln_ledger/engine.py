"""状态判定引擎。

在每条维护线的可达提交子图上，按拓扑序逐节点推导每个受认可修复标识的状态：
active（生效）/ inactive（撤回）/ unknown（矛盾未解）/ absent（从未出现）。
"""
from __future__ import annotations

from collections import defaultdict

from .graph import ancestors_bfs, reachable_order, topological_order
from .models import (
    LedgerInput,
    MaintenanceLine,
    NodeState,
    STATE_ABSENT,
    STATE_ACTIVE,
    STATE_INACTIVE,
    STATE_UNKNOWN,
    STATUS_AFFECTED,
    STATUS_FIXED,
    STATUS_LABELS,
    STATUS_REAFFECTED,
    STATUS_UNKNOWN,
)


def _resolution_index(ledger: LedgerInput, line_name: str) -> dict:
    """按提交聚合对本维护线生效的解决记录，行级记录优先于全局记录。"""
    index: dict = defaultdict(list)
    for res in ledger.resolutions:
        if res.line is not None and res.line != line_name:
            continue
        index[res.commit].append(
            {
                "fix_id": res.fix_id,
                "resolved": res.resolved,
                "scope": "line" if res.line is not None else "global",
            }
        )
    return index


def _combine_parent_states(cid: str, fid: str, parent_states: list) -> NodeState:
    """合并多个父线对同一修复标识给出的状态。

    合并节点把不同父线汇入同一历史：只要父线之间出现不同的非未知结论
    （生效 / 撤回 / 从未出现），且没有显式解决记录，就保留未知。
    """
    # 可达子图中的每个父提交都有该标识的状态（absent 也算一种结论）
    present = [(pid, st) for pid, st in parent_states if st is not None]
    if not present:
        return NodeState(STATE_ABSENT)

    unknown_parents = [(pid, st) for pid, st in present if st.state == STATE_UNKNOWN]
    if unknown_parents:
        reasons: list = []
        for pid, st in unknown_parents:
            reasons.extend(st.reasons)
            reasons.append(
                {"code": "unknown_propagated", "commit": cid, "fix_id": fid, "parent": pid}
            )
        return NodeState.unknown(reasons)

    active_parents = sorted(pid for pid, st in present if st.state == STATE_ACTIVE)
    inactive_parents = sorted(pid for pid, st in present if st.state == STATE_INACTIVE)
    absent_parents = sorted(pid for pid, st in present if st.state == STATE_ABSENT)
    distinct = {st.state for _, st in present}

    if len(distinct) > 1:
        reason = {
            "code": "merge_conflict",
            "commit": cid,
            "fix_id": fid,
            "active_parents": active_parents,
            "inactive_parents": inactive_parents,
            "absent_parents": absent_parents,
        }
        return NodeState.unknown([reason])

    only = next(iter(distinct))
    if only == STATE_ACTIVE:
        return NodeState.active()
    if only == STATE_INACTIVE:
        return NodeState.inactive()
    return NodeState(STATE_ABSENT)


def evaluate_line(ledger: LedgerInput, line: MaintenanceLine) -> dict:
    """推导一条维护线上所有可达节点的修复状态。"""
    _, reachable, _children = reachable_order(ledger.commits, line.roots)
    topo = topological_order(ledger.commits, reachable)
    res_index = _resolution_index(ledger, line.name)
    recognized = list(line.recognized_fix_ids)
    recognized_set = set(recognized)

    node_states: dict = {}
    node_events: dict = defaultdict(list)
    anomalies: list = []
    encountered: set = set()
    root_set = set(line.roots)

    for cid in topo:
        commit = ledger.commits[cid]
        in_line_parents = [p for p in commit.parents if p in reachable]
        states_here: dict = {}

        for fid in recognized:
            parent_states = [
                (pid, node_states[pid].get(fid))
                for pid in in_line_parents
                if pid in node_states
            ]
            if not in_line_parents:
                # 维护线声明的根提交是历史基线；没有可达父提交又不是根的情况
                # 理论上不会出现在可达子图里，防御性按基线处理。
                states_here[fid] = NodeState(STATE_ABSENT)
                if cid in root_set:
                    node_events[cid].append(
                        {"type": "baseline", "commit": cid, "fix_id": fid, "result": STATE_ABSENT}
                    )
                continue

            merged = _combine_parent_states(cid, fid, parent_states)
            states_here[fid] = merged
            if len(in_line_parents) > 1:
                node_events[cid].append(
                    {
                        "type": "merge",
                        "commit": cid,
                        "fix_id": fid,
                        "result": merged.state,
                        "parent_states": {
                            pid: (st.state if st is not None else STATE_ABSENT)
                            for pid, st in parent_states
                        },
                    }
                )

        # 显式解决记录：行级优先、通配（fix_id 为 null）作用于全部受认可标识
        if cid in res_index:
            entries = sorted(
                res_index[cid],
                key=lambda e: (
                    0 if e["scope"] == "line" else 1,
                    1 if e["fix_id"] is None else 0,
                ),
            )
            if len(commit.parents) < 2:
                anomalies.append(
                    {"code": "resolution_on_non_merge", "commit": cid, "line": line.name}
                )
            for entry in entries:
                targets = recognized if entry["fix_id"] is None else [entry["fix_id"]]
                for fid in targets:
                    if fid not in recognized_set:
                        anomalies.append(
                            {
                                "code": "resolution_unrecognized_fix",
                                "commit": cid,
                                "line": line.name,
                                "fix_id": fid,
                            }
                        )
                        continue
                    previous = states_here[fid]
                    states_here[fid] = (
                        NodeState.active()
                        if entry["resolved"] == "fixed"
                        else NodeState.inactive()
                    )
                    node_events[cid].append(
                        {
                            "type": "resolution",
                            "commit": cid,
                            "fix_id": fid,
                            "resolved": entry["resolved"],
                            "scope": entry["scope"],
                            "cleared_unknown": previous.state == STATE_UNKNOWN,
                            "cleared_reasons": (
                                previous.reasons if previous.state == STATE_UNKNOWN else []
                            ),
                        }
                    )

        # 本节点认可的修复提交
        for fid in commit.fix_ids:
            if fid in recognized_set:
                states_here[fid] = NodeState.active()
                node_events[cid].append({"type": "fix", "commit": cid, "fix_id": fid})
            else:
                anomalies.append(
                    {
                        "code": "unrecognized_fix_record",
                        "commit": cid,
                        "line": line.name,
                        "fix_id": fid,
                    }
                )
                node_events[cid].append(
                    {
                        "type": "ignored",
                        "commit": cid,
                        "fix_id": fid,
                        "reason": "unrecognized_fix",
                    }
                )

        # 撤回记录
        for ref in commit.revert_refs:
            if ref is None:
                active_now = [
                    fid for fid in recognized if states_here[fid].state == STATE_ACTIVE
                ]
                if not active_now:
                    anomalies.append(
                        {
                            "code": "bare_revert_without_active_fix",
                            "commit": cid,
                            "line": line.name,
                        }
                    )
                    node_events[cid].append(
                        {
                            "type": "ignored",
                            "commit": cid,
                            "fix_id": None,
                            "reason": "bare_revert_no_active_fix",
                        }
                    )
                for fid in active_now:
                    states_here[fid] = NodeState.inactive()
                    node_events[cid].append(
                        {"type": "revert", "commit": cid, "fix_id": fid, "bare": True}
                    )
                continue

            if ref not in recognized_set:
                anomalies.append(
                    {
                        "code": "unrecognized_revert_record",
                        "commit": cid,
                        "line": line.name,
                        "fix_id": ref,
                    }
                )
                node_events[cid].append(
                    {
                        "type": "ignored",
                        "commit": cid,
                        "fix_id": ref,
                        "reason": "unrecognized_revert",
                    }
                )
                continue

            current = states_here[ref]
            if current.state == STATE_UNKNOWN:
                node_events[cid].append(
                    {
                        "type": "ignored",
                        "commit": cid,
                        "fix_id": ref,
                        "reason": "revert_while_unknown",
                    }
                )
                continue
            if current.state == STATE_ABSENT:
                anomalies.append(
                    {
                        "code": "revert_without_prior_fix",
                        "commit": cid,
                        "line": line.name,
                        "fix_id": ref,
                    }
                )
                node_events[cid].append(
                    {
                        "type": "ignored",
                        "commit": cid,
                        "fix_id": ref,
                        "reason": "revert_without_prior_fix",
                    }
                )
                continue
            states_here[ref] = NodeState.inactive()
            node_events[cid].append(
                {"type": "revert", "commit": cid, "fix_id": ref, "bare": False}
            )

        for fid, st in states_here.items():
            if st.state != STATE_ABSENT:
                encountered.add(fid)
        node_states[cid] = states_here

    return {
        "line": line.name,
        "reachable": reachable,
        "topo": topo,
        "node_states": node_states,
        "node_events": dict(node_events),
        "anomalies": anomalies,
        "encountered": encountered,
    }


def _fix_status(state: NodeState, encountered: bool) -> str:
    if state.state == STATE_UNKNOWN:
        return STATUS_UNKNOWN
    if state.state == STATE_ACTIVE:
        return STATUS_FIXED
    # inactive：出现过即“生效后被撤回”
    return STATUS_REAFFECTED if encountered else STATUS_AFFECTED


def evaluate_build(ledger: LedgerInput, line_eval: dict, build) -> dict:
    """依据维护线推导结果生成单个部署的结论、依据链与缺失记录。"""
    line: MaintenanceLine = ledger.lines[build.line]
    recognized = list(line.recognized_fix_ids)
    ancestors = set(ancestors_bfs(ledger.commits, build.source_commit))

    missing: list = []
    if build.source_commit not in line_eval["reachable"]:
        missing.append(
            {
                "code": "source_not_reachable_from_line",
                "build_id": build.build_id,
                "line": build.line,
                "source_commit": build.source_commit,
                "line_roots": line.roots,
            }
        )
        return {
            "build_id": build.build_id,
            "line": build.line,
            "source_commit": build.source_commit,
            "status": STATUS_UNKNOWN,
            "status_label": STATUS_LABELS[STATUS_UNKNOWN],
            "fix_statuses": {
                fid: {"state": STATE_UNKNOWN, "status": STATUS_UNKNOWN, "encountered": False}
                for fid in recognized
            },
            "evidence_chain": [],
            "missing_records": missing,
        }

    source_states: dict = line_eval["node_states"][build.source_commit]
    # “是否曾出现”按该部署的祖先链判断，而不是整条维护线
    encountered_on_path: set = set()
    for anc in ancestors:
        for fid, st in line_eval["node_states"][anc].items():
            if st.state != STATE_ABSENT:
                encountered_on_path.add(fid)

    fix_statuses: dict = {}
    for fid in recognized:
        st = source_states[fid]
        encountered = fid in encountered_on_path
        fix_statuses[fid] = {
            "state": st.state,
            "status": _fix_status(st, encountered),
            "encountered": encountered,
        }
        if st.state == STATE_UNKNOWN:
            for reason in st.reasons:
                payload = {k: v for k, v in reason.items() if k != "code"}
                missing.append(
                    {"code": reason["code"], "build_id": build.build_id, "fix_id": fid, **payload}
                )
                if reason["code"] == "merge_conflict":
                    missing.append(
                        {
                            "code": "missing_merge_resolution",
                            "build_id": build.build_id,
                            "commit": reason["commit"],
                            "fix_id": fid,
                        }
                    )

    # 仅保留该部署祖先链上的异常作为缺失/无法判定记录
    for anomaly in line_eval["anomalies"]:
        if anomaly.get("commit") in ancestors:
            missing.append({"build_id": build.build_id, **anomaly})

    statuses = {item["status"] for item in fix_statuses.values()}
    if STATUS_UNKNOWN in statuses:
        overall = STATUS_UNKNOWN
    elif not any(item["encountered"] for item in fix_statuses.values()):
        overall = STATUS_AFFECTED
    elif STATUS_REAFFECTED in statuses:
        overall = STATUS_REAFFECTED
    else:
        overall = STATUS_FIXED

    evidence: list = []
    for cid in line_eval["topo"]:
        if cid not in ancestors:
            continue
        commit = ledger.commits[cid]
        evidence.append(
            {
                "commit": cid,
                "parents": list(commit.parents),
                "events": list(line_eval["node_events"].get(cid, [])),
                "states": {fid: line_eval["node_states"][cid][fid].state for fid in recognized},
            }
        )

    return {
        "build_id": build.build_id,
        "line": build.line,
        "source_commit": build.source_commit,
        "status": overall,
        "status_label": STATUS_LABELS[overall],
        "fix_statuses": fix_statuses,
        "evidence_chain": evidence,
        "missing_records": missing,
    }


def evaluate(ledger: LedgerInput) -> dict:
    """评估全部部署，按维护线缓存推导结果。"""
    line_evals: dict = {}
    deployments = []
    for build in ledger.builds:
        if build.line not in line_evals:
            line_evals[build.line] = evaluate_line(ledger, ledger.lines[build.line])
        deployments.append(evaluate_build(ledger, line_evals[build.line], build))

    return {"schema_version": "1.0", "deployments": deployments}
