"""输入加载与结构校验。

支持两种输入方式：
1. 单个 bundle 文件：{"fix": ..., "deployments": ..., "regression": ..., "machines": ...}
2. 四个独立文件：fix / deployments / regression / machines

所有输入均为本地合成 JSON 记录，不访问任何外部系统。
"""

import json


class LoaderError(ValueError):
    """输入文件缺失、格式非法或字段校验失败。"""


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise LoaderError("输入文件不存在: %s" % path)
    except json.JSONDecodeError as exc:
        raise LoaderError("输入文件不是合法 JSON: %s (%s)" % (path, exc))


def load_bundle(path):
    """从单个 bundle 文件加载四部分数据。"""
    data = _load_json(path)
    if not isinstance(data, dict):
        raise LoaderError("bundle 顶层必须是 JSON 对象: %s" % path)
    missing = [k for k in ("fix", "deployments", "regression", "machines") if k not in data]
    if missing:
        raise LoaderError("bundle 缺少必需键: %s" % ", ".join(sorted(missing)))
    return validate(data["fix"], data["deployments"], data["regression"], data["machines"])


def load_parts(fix_path, deployments_path, regression_path, machines_path):
    """从四个独立文件加载数据。"""
    return validate(
        _load_json(fix_path),
        _load_json(deployments_path),
        _load_json(regression_path),
        _load_json(machines_path),
    )


def _require(cond, msg):
    if not cond:
        raise LoaderError(msg)


def _require_str(obj, field, where):
    _require(isinstance(obj.get(field), str) and obj[field].strip(),
             "%s 缺少非空字符串字段 '%s'" % (where, field))


def validate(fix, deployments, regression, machines):
    """校验四部分输入结构，返回规范化字典。"""
    # --- fix ---
    _require(isinstance(fix, dict), "fix 必须是 JSON 对象")
    _require_str(fix, "ticket", "fix")
    _require_str(fix, "fixed_at", "fix")
    components = fix.get("components")
    _require(isinstance(components, list) and components, "fix.components 必须是非空数组")
    seen = set()
    for i, comp in enumerate(components):
        where = "fix.components[%d]" % i
        _require(isinstance(comp, dict), "%s 必须是对象" % where)
        _require_str(comp, "name", where)
        _require_str(comp, "fixed_artifact", where)
        _require(comp["name"] not in seen, "组件重复定义: %s" % comp["name"])
        seen.add(comp["name"])

    # --- deployments ---
    _require(isinstance(deployments, dict), "deployments 必须是 JSON 对象")
    instances = deployments.get("instances")
    _require(isinstance(instances, list), "deployments.instances 必须是数组")
    inst_ids = set()
    for i, inst in enumerate(instances):
        where = "deployments.instances[%d]" % i
        _require(isinstance(inst, dict), "%s 必须是对象" % where)
        for field in ("instance_id", "component", "environment", "artifact",
                      "machine_id", "state", "commissioned_at"):
            _require_str(inst, field, where)
        _require(inst["state"] in ("active", "decommissioned"),
                 "%s.state 必须是 active 或 decommissioned" % where)
        _require(inst["instance_id"] not in inst_ids,
                 "实例 ID 重复: %s" % inst["instance_id"])
        inst_ids.add(inst["instance_id"])
        if inst["state"] == "decommissioned":
            _require_str(inst, "decommissioned_at", where + "(已下线实例)")

    # --- regression ---
    _require(isinstance(regression, dict), "regression 必须是 JSON 对象")
    results = regression.get("results")
    _require(isinstance(results, list), "regression.results 必须是数组")
    for i, res in enumerate(results):
        where = "regression.results[%d]" % i
        _require(isinstance(res, dict), "%s 必须是对象" % where)
        for field in ("check_id", "component", "env_category", "artifact",
                      "phase", "result", "run_at"):
            _require_str(res, field, where)
        _require(res["phase"] in ("pre_fix", "post_fix"),
                 "%s.phase 必须是 pre_fix 或 post_fix" % where)
        _require(res["result"] in ("pass", "fail"),
                 "%s.result 必须是 pass 或 fail" % where)

    # --- machines ---
    _require(isinstance(machines, dict), "machines 必须是 JSON 对象")
    machine_list = machines.get("machines")
    _require(isinstance(machine_list, list), "machines.machines 必须是数组")
    machine_ids = set()
    for i, mach in enumerate(machine_list):
        where = "machines.machines[%d]" % i
        _require(isinstance(mach, dict), "%s 必须是对象" % where)
        for field in ("machine_id", "hostname", "environment", "status"):
            _require_str(mach, field, where)
        _require(mach["status"] in ("online", "offline", "decommissioned"),
                 "%s.status 必须是 online/offline/decommissioned" % where)
        _require(mach["machine_id"] not in machine_ids,
                 "机器 ID 重复: %s" % mach["machine_id"])
        machine_ids.add(mach["machine_id"])

    return {
        "fix": fix,
        "deployments": {"instances": instances},
        "regression": {"results": results},
        "machines": {"machines": machine_list},
    }
