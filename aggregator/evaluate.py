"""核心判定逻辑。

按 (组件, 环境) 输出三态结论：
- pending_deploy  (待部署)：仍有在役实例未运行修复产物，或回归证据只覆盖候选产物而非实际部署产物
- pending_verify  (待验证)：部署已到位，但回归证据无法配对 / 回归失败 / 新上线实例未被覆盖
- closable        (可关闭)：全部在役实例运行修复产物，且修复前后回归证据同检查定义同环境类别配对通过

规则要点：
- 只认实际部署的产物身份：post_fix 通过仅当 artifact 等于在役实例实际部署的修复产物才有效
- 修复前后结果必须来自同一 check_id 且同一 env_category，无法配对的列入 missing
- 已下线实例的证据与修复完成分开统计，不计入关闭依据
- fixed_at 之后新上线的实例，必须有 run_at 不早于其 commissioned_at 的有效 post_fix 通过记录
"""

from datetime import datetime, timezone

STATUS_CLOSABLE = "closable"
STATUS_PENDING_DEPLOY = "pending_deploy"
STATUS_PENDING_VERIFY = "pending_verify"

STATUS_LABELS = {
    STATUS_CLOSABLE: "可关闭",
    STATUS_PENDING_DEPLOY: "待部署",
    STATUS_PENDING_VERIFY: "待验证",
}


def _parse_ts(value):
    """解析 ISO-8601 时间戳；非法时返回 None（由调用方按无证据处理）。"""
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _ev(kind, ref, detail):
    return {"type": kind, "ref": ref, "detail": detail}


def evaluate(data):
    """对加载后的输入执行判定，返回报告字典。"""
    fix = data["fix"]
    ticket = fix["ticket"]
    fixed_at = _parse_ts(fix["fixed_at"])
    components = {c["name"]: c["fixed_artifact"] for c in fix["components"]}

    machines = {m["machine_id"]: m for m in data["machines"]["machines"]}
    instances = data["deployments"]["instances"]
    results = data["regression"]["results"]

    warnings = []

    # 机器清单交叉核对：机器已下线 => 其上的实例证据同样按下线处理
    inst_machine = {}
    for inst in instances:
        mach = machines.get(inst["machine_id"])
        if mach is None:
            warnings.append("实例 %s 引用的机器 %s 不在机器清单中，按无法核实处理"
                            % (inst["instance_id"], inst["machine_id"]))
        inst_machine[inst["instance_id"]] = mach

    def instance_effective_state(inst):
        if inst["state"] == "decommissioned":
            return "decommissioned"
        mach = inst_machine[inst["instance_id"]]
        if mach is None:
            return "unknown"
        if mach["status"] == "decommissioned":
            return "decommissioned"
        if mach["status"] == "offline":
            return "offline"
        return "active"

    decisions = []
    for comp_name in sorted(components):
        fixed_artifact = components[comp_name]
        comp_instances = [i for i in instances if i["component"] == comp_name]
        comp_results = [r for r in results if r["component"] == comp_name]

        envs = sorted({i["environment"] for i in comp_instances}
                      | {r["env_category"] for r in comp_results})
        for env in envs:
            decisions.append(_evaluate_env(
                ticket, comp_name, env, fixed_artifact, fixed_at,
                [i for i in comp_instances if i["environment"] == env],
                [r for r in comp_results if r["env_category"] == env],
                inst_machine, instance_effective_state, warnings,
            ))

    # 引用了未在修复清单中的组件的证据，提示但不参与判定
    in_scope = set(components)
    for inst in instances:
        if inst["component"] not in in_scope:
            warnings.append("实例 %s 的组件 %s 不在修复清单中，已忽略"
                            % (inst["instance_id"], inst["component"]))
    for res in results:
        if res["component"] not in in_scope:
            warnings.append("回归结果 %s 的组件 %s 不在修复清单中，已忽略"
                            % (res["check_id"], res["component"]))

    return {
        "ticket": ticket,
        "generated_by": "evidence-aggregator",
        "decisions": decisions,
        "warnings": warnings,
    }


def _evaluate_env(ticket, comp, env, fixed_artifact, fixed_at,
                  env_instances, env_results, inst_machine,
                  effective_state, warnings):
    chain = [_ev("fix", "%s/%s" % (ticket, comp),
                 "修复产物身份 %s，修复时间 %s" % (fixed_artifact,
                                                 fixed_at.isoformat() if fixed_at else "未知"))]
    missing = []
    active, decommissioned, unverifiable = [], [], []
    for inst in sorted(env_instances, key=lambda i: i["instance_id"]):
        state = effective_state(inst)
        mach = inst_machine[inst["instance_id"]]
        mach_desc = ("机器 %s(%s) 状态 %s" % (mach["machine_id"], mach["hostname"], mach["status"])
                     if mach else "机器 %s 不在清单中" % inst["machine_id"])
        if state == "active":
            active.append(inst)
            chain.append(_ev("deployment", inst["instance_id"],
                             "在役实例部署产物 %s；%s" % (inst["artifact"], mach_desc)))
        elif state == "decommissioned":
            decommissioned.append(inst)
            chain.append(_ev("decommissioned", inst["instance_id"],
                             "实例已下线（%s），其证据单独统计、不计入修复完成" % mach_desc))
        else:
            unverifiable.append(inst)
            chain.append(_ev("unverifiable", inst["instance_id"],
                             "实例状态无法核实（%s）" % mach_desc))

    # 新上线实例：fixed_at 之后上线
    new_instances = []
    if fixed_at is not None:
        for inst in active:
            commissioned = _parse_ts(inst["commissioned_at"])
            if commissioned is not None and commissioned > fixed_at:
                new_instances.append(inst)

    # 回归结果按 check_id 分组配对（同检查定义 + 同环境类别，env 已在分组条件中）
    checks = {}
    for res in sorted(env_results, key=lambda r: (r["check_id"], r["run_at"])):
        checks.setdefault(res["check_id"], {"pre_fix": [], "post_fix": []})[res["phase"]].append(res)

    paired_ok, paired_fail, candidate_only = [], [], []
    for check_id in sorted(checks):
        group = checks[check_id]
        pre, post = group["pre_fix"], group["post_fix"]
        if pre and not post:
            missing.append("检查 %s 缺少修复后(post_fix)结果" % check_id)
            chain.append(_ev("missing", check_id, "存在修复前结果但无同检查定义同环境的修复后结果"))
            continue
        if post and not pre:
            missing.append("检查 %s 缺少修复前(pre_fix)基线结果" % check_id)
            chain.append(_ev("missing", check_id, "存在修复后结果但无同检查定义同环境的修复前基线"))
            continue
        # 已配对：post_fix 通过仅当产物身份 == 实际部署的修复产物才有效
        valid_pass = [r for r in post if r["result"] == "pass" and r["artifact"] == fixed_artifact]
        if valid_pass:
            paired_ok.append(check_id)
            chain.append(_ev("regression", check_id,
                             "修复前后结果配对（同检查定义、同环境类别 %s），"
                             "修复后于实际部署产物 %s 通过" % (env, fixed_artifact)))
        elif any(r["result"] == "fail" for r in post):
            paired_fail.append(check_id)
            chain.append(_ev("regression", check_id, "修复后回归结果失败"))
        else:
            candidate_only.append(check_id)
            tested = sorted({r["artifact"] for r in post})
            chain.append(_ev("candidate_only", check_id,
                             "修复后通过仅覆盖候选产物 %s，非实际部署产物 %s"
                             % ("、".join(tested), fixed_artifact)))

    # 新上线实例覆盖核对：需存在 run_at >= commissioned_at 的有效 post_fix 通过
    unverified_new = []
    for inst in new_instances:
        commissioned = _parse_ts(inst["commissioned_at"])
        covered = any(
            r["phase"] == "post_fix" and r["result"] == "pass"
            and r["artifact"] == fixed_artifact
            and (_parse_ts(r["run_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= commissioned
            for r in env_results
        )
        if covered:
            chain.append(_ev("new_instance", inst["instance_id"],
                             "新上线实例已有不早于上线时间的修复后通过证据"))
        else:
            unverified_new.append(inst)
            missing.append("新上线实例 %s 缺少上线后的修复回归通过证据" % inst["instance_id"])
            chain.append(_ev("new_instance", inst["instance_id"],
                             "新上线实例缺少不早于 %s 的修复后通过证据" % inst["commissioned_at"]))

    # ---- 状态判定 ----
    outdated = [i for i in active if i["artifact"] != fixed_artifact]
    reasons = []
    if not active and not unverifiable:
        if paired_fail or missing or candidate_only:
            status = STATUS_PENDING_VERIFY
            reasons.append("该环境无在役实例，但回归证据存在缺口（缺项 %d 项）" % len(missing))
        else:
            status = STATUS_CLOSABLE
            reasons.append("该环境无在役实例（%d 个实例已下线，单独统计）" % len(decommissioned))
    elif unverifiable:
        status = STATUS_PENDING_VERIFY
        reasons.append("实例 %s 所在机器无法核实，需人工确认"
                       % "、".join(i["instance_id"] for i in unverifiable))
    elif outdated:
        status = STATUS_PENDING_DEPLOY
        reasons.append("在役实例 %s 仍运行旧产物，修复未部署"
                       % "、".join(i["instance_id"] for i in outdated))
    elif candidate_only:
        status = STATUS_PENDING_DEPLOY
        reasons.append("回归通过仅覆盖候选产物，未在实际部署产物上验证")
    elif paired_fail or missing:
        status = STATUS_PENDING_VERIFY
        if paired_fail:
            reasons.append("检查 %s 修复后回归失败" % "、".join(paired_fail))
        if missing:
            reasons.append("存在无法配对的检查项或新实例证据缺口")
    else:
        status = STATUS_CLOSABLE
        reasons.append("全部在役实例运行修复产物，修复前后回归证据配对通过")

    chain.append(_ev("conclusion", "%s/%s" % (comp, env),
                     "%s：%s" % (STATUS_LABELS[status], "；".join(reasons))))

    return {
        "component": comp,
        "environment": env,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reasons": reasons,
        "summary": {
            "active_instances": len(active),
            "decommissioned_instances": len(decommissioned),
            "unverifiable_instances": len(unverifiable),
            "new_instances": len(new_instances),
            "checks_paired_ok": len(paired_ok),
            "checks_failed": len(paired_fail),
            "checks_candidate_only": len(candidate_only),
        },
        "decommissioned_instances": [
            {"instance_id": i["instance_id"], "artifact": i["artifact"],
             "decommissioned_at": i.get("decommissioned_at")}
            for i in decommissioned
        ],
        "new_instances": [
            {"instance_id": i["instance_id"], "commissioned_at": i["commissioned_at"],
             "verified": i not in unverified_new}
            for i in new_instances
        ],
        "missing": missing,
        "evidence_chain": chain,
    }
