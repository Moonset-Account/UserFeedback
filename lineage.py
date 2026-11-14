"""字段血缘分级核心逻辑（仅使用 Python 标准库）。

等级约定：manifest["levels"] 按从低到高排列，缺省 ["S1", "S2", "S3", "S4"]。
派生规则：一般派生取输入中的最高等级；只有明确记录且状态为 approved 的
降级规则，在其批准范围（具体输出字段 + 具体输入等级）完全匹配时，
才可将输出降到规则指定的目标级别。
"""

DEFAULT_LEVELS = ["S1", "S2", "S3", "S4"]
STATUS_OK = "ok"
STATUS_PENDING = "pending_review"


class ManifestError(ValueError):
    """清单格式或内容非法。"""


def load_manifest(path):
    """从 JSON 文件载入并校验清单。"""
    import json

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ManifestError("清单顶层必须是 JSON 对象")
    return validate_manifest(data)


def validate_manifest(data):
    """校验并规范化清单字典，返回规范化后的 manifest。"""
    levels = data.get("levels", DEFAULT_LEVELS)
    if (
        not isinstance(levels, list)
        or not levels
        or any(not isinstance(x, str) for x in levels)
        or len(set(levels)) != len(levels)
    ):
        raise ManifestError("levels 必须是非空、不重复的字符串列表（按低->高排列）")

    raw_fields = {}
    for name, info in (data.get("fields") or {}).items():
        lvl = info.get("level") if isinstance(info, dict) else info
        if lvl not in levels:
            raise ManifestError("字段 %s 的等级 %r 不在 levels 中" % (name, lvl))
        raw_fields[name] = lvl

    edges = []
    for e in data.get("edges") or []:
        srcs = e.get("from")
        dst = e.get("to")
        if not isinstance(srcs, list) or not srcs or not dst:
            raise ManifestError("每条 edge 需要非空 from 列表和 to 字段")
        edges.append({"from": list(srcs), "to": dst})

    downgrades = []
    for d in data.get("downgrades") or []:
        for key in ("id", "output", "inputs", "target"):
            if key not in d:
                raise ManifestError("降级规则缺少字段: %s" % key)
        if d["target"] not in levels:
            raise ManifestError("降级规则 %s 的 target 不在 levels 中" % d["id"])
        if not isinstance(d["inputs"], dict) or not d["inputs"]:
            raise ManifestError("降级规则 %s 的 inputs 必须是非空对象" % d["id"])
        for inp, lvl in d["inputs"].items():
            if lvl not in levels:
                raise ManifestError(
                    "降级规则 %s 的输入等级 %r 不在 levels 中" % (d["id"], lvl)
                )
        downgrades.append(
            {
                "id": d["id"],
                "output": d["output"],
                "inputs": dict(d["inputs"]),
                "target": d["target"],
                "status": d.get("status", "approved"),
                "reason": d.get("reason", ""),
            }
        )
    ids = [d["id"] for d in downgrades]
    if len(set(ids)) != len(ids):
        raise ManifestError("降级规则 id 重复")

    return {
        "levels": list(levels),
        "fields": raw_fields,
        "edges": edges,
        "downgrades": downgrades,
    }


def find_cycles(inputs_of):
    """在血缘图上找强连通分量，返回成环字段分组（迭代 Tarjan）。"""
    nodes = set(inputs_of)
    for ins in inputs_of.values():
        nodes.update(ins)
    graph = {n: list(inputs_of.get(n, [])) for n in nodes}

    index_of = {}
    lowlink = {}
    on_stack = set()
    stack = []
    sccs = []
    counter = [0]

    for root in sorted(nodes):
        if root in index_of:
            continue
        work = [(root, 0)]
        while work:
            node, ci = work[-1]
            if ci == 0:
                index_of[node] = lowlink[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            neighbors = graph[node]
            for j in range(ci, len(neighbors)):
                nxt = neighbors[j]
                if nxt not in index_of:
                    work[-1] = (node, j + 1)
                    work.append((nxt, 0))
                    recursed = True
                    break
                elif nxt in on_stack:
                    if index_of[nxt] < lowlink[node]:
                        lowlink[node] = index_of[nxt]
            if recursed:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                if lowlink[node] < lowlink[parent]:
                    lowlink[parent] = lowlink[node]
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(scc)

    cycles = []
    for scc in sccs:
        if len(scc) > 1 or scc[0] in graph[scc[0]]:
            cycles.append(sorted(scc))
    cycles.sort()
    return cycles


def classify(manifest):
    """按清单计算每个字段的分级，返回报告字典。

    报告结构：
      levels: 等级序列（低->高）
      fields: {字段: {level, status, in_cycle, explanation}}
      cycles: 成环字段分组列表
      pending_review: 待核对字段列表
      downgrades: {applied, unmatched:[{id, output, reason}], revoked}
    """
    levels = manifest["levels"]
    rank = {lv: i for i, lv in enumerate(levels)}
    raw = manifest["fields"]

    inputs_of = {}
    for e in manifest["edges"]:
        inputs_of.setdefault(e["to"], [])
        for s in e["from"]:
            if s not in inputs_of[e["to"]]:
                inputs_of[e["to"]].append(s)

    all_fields = set(raw) | set(inputs_of)
    for ins in inputs_of.values():
        all_fields.update(ins)
    for d in manifest["downgrades"]:
        all_fields.add(d["output"])
        all_fields.update(d["inputs"])

    lowest = levels[0]
    level = {}
    pending = {}
    for f in all_fields:
        if f in inputs_of:
            level[f] = lowest  # 派生字段从最低级开始向上迭代
            pending[f] = False
        elif f in raw:
            level[f] = raw[f]
            pending[f] = False
        else:
            level[f] = None  # 血缘缺边且无原始等级 -> 待核对
            pending[f] = True

    approved = [d for d in manifest["downgrades"] if d["status"] == "approved"]
    revoked = [d["id"] for d in manifest["downgrades"] if d["status"] != "approved"]
    applied = {f: None for f in inputs_of}

    # 迭代到稳定：等级关于输入单调不减且取值有界，必然收敛；
    # 环内字段随迭代一同抬升到稳定值。
    max_iter = (len(all_fields) + 1) * (len(levels) + 2) + 10
    for _ in range(max_iter):
        changed = False
        for f in sorted(inputs_of):
            ins = inputs_of[f]
            p = any(pending[i] for i in ins)
            known = [level[i] for i in ins if level[i] is not None]
            base = max(known, key=lambda lv: rank[lv]) if known else None
            new_level = base
            rule_used = None
            if base is not None:
                matching = [
                    d
                    for d in approved
                    if d["output"] == f
                    and all(
                        i in ins and level.get(i) == exp
                        for i, exp in d["inputs"].items()
                    )
                ]
                if matching:
                    # 多条命中时取目标等级最低者，并列取 id 较小者，保证确定性
                    matching.sort(key=lambda d: (rank[d["target"]], d["id"]))
                    rule_used = matching[0]
                    new_level = rule_used["target"]
            applied[f] = rule_used["id"] if rule_used else None
            if new_level != level[f] or p != pending[f]:
                level[f] = new_level
                pending[f] = p
                changed = True
        if not changed:
            break
    else:
        raise ManifestError("等级迭代未收敛，请检查清单")

    cycles = find_cycles(inputs_of)
    in_cycle = {f for c in cycles for f in c}

    fields_report = {}
    for f in sorted(all_fields):
        if f in inputs_of:
            ins = inputs_of[f]
            known = [(i, level[i]) for i in ins if level[i] is not None]
            src = None
            base = None
            if known:
                base = max((lv for _, lv in known), key=lambda lv: rank[lv])
                src = sorted(i for i, lv in known if lv == base)[0]
            rid = applied.get(f)
            if rid:
                text = "按已批准降级规则 %s 降至 %s（输入最高等级 %s，来自 %s）" % (
                    rid,
                    level[f],
                    base,
                    src,
                )
            elif src is not None:
                text = "继承输入最高等级 %s，来自 %s" % (level[f], src)
            else:
                text = "血缘输入等级均未知，待核对"
            if pending[f]:
                bad = sorted(i for i in ins if pending[i])
                text += "；输入 %s 待核对" % "、".join(bad)
        elif f in raw:
            text = "原始字段，清单直接给定等级 %s" % level[f]
        else:
            text = "血缘缺边且无原始等级，待核对"
        fields_report[f] = {
            "level": level[f],
            "status": STATUS_PENDING if pending[f] else STATUS_OK,
            "in_cycle": f in in_cycle,
            "explanation": text,
        }

    applied_ids = {rid for rid in applied.values() if rid}
    unmatched = []
    for d in approved:
        if d["id"] in applied_ids:
            continue
        out = d["output"]
        if out not in inputs_of:
            reason = "输出字段 %s 无血缘边，规则不适用" % out
        else:
            parts = []
            for inp, exp in d["inputs"].items():
                if inp not in inputs_of[out]:
                    parts.append("输入 %s 不在 %s 的血缘中" % (inp, out))
                elif level.get(inp) != exp:
                    parts.append(
                        "输入 %s 期望等级 %s，实际 %s" % (inp, exp, level.get(inp))
                    )
            reason = "；".join(parts) if parts else "规则未命中"
        unmatched.append({"id": d["id"], "output": out, "reason": reason})

    return {
        "levels": list(levels),
        "fields": fields_report,
        "cycles": cycles,
        "pending_review": sorted(f for f in all_fields if pending[f]),
        "downgrades": {
            "applied": sorted(applied_ids),
            "unmatched": unmatched,
            "revoked": sorted(revoked),
        },
    }


def revoke_rule(manifest, rule_id):
    """返回撤销指定降级规则后的新清单（原清单不被修改）。"""
    found = False
    new_downgrades = []
    for d in manifest["downgrades"]:
        d2 = dict(d)
        d2["inputs"] = dict(d["inputs"])
        if d2["id"] == rule_id:
            d2["status"] = "revoked"
            found = True
        new_downgrades.append(d2)
    if not found:
        raise ManifestError("未找到降级规则: %s" % rule_id)
    updated = dict(manifest)
    updated["downgrades"] = new_downgrades
    return updated


def diff_reports(before, after):
    """对比两份分级报告，列出等级或状态发生变化的字段（受影响的下游）。"""
    affected = []
    for f in sorted(after["fields"]):
        a = after["fields"][f]
        b = before["fields"].get(f)
        key_a = (a["level"], a["status"])
        key_b = (b["level"], b["status"]) if b else None
        if key_a != key_b:
            affected.append(
                {
                    "field": f,
                    "before": None
                    if b is None
                    else {"level": b["level"], "status": b["status"]},
                    "after": {"level": a["level"], "status": a["status"]},
                }
            )
    return affected
