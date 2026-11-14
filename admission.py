"""容器发布准入检查核心逻辑（纯 Python 3 标准库）。

输入：候选工作负载 JSON、可选已上线版本、团队基线、审批例外。
输出：可供流水线消费的放行/拦截结果（字典，由 app.py 序列化为 JSON）。
"""

from datetime import datetime, timezone

# 违规类型
PRIVILEGED = "privileged"
RUN_AS_ROOT = "run-as-root"
HOST_PATH = "host-path"
RUN_IDENTITY_UNCONFIRMED = "run-identity-unconfirmed"

ALL_VIOLATION_TYPES = (PRIVILEGED, RUN_AS_ROOT, HOST_PATH, RUN_IDENTITY_UNCONFIRMED)

# 团队基线默认值：三项检查全部强制执行，无放行宿主机路径
DEFAULT_BASELINE = {
    "team": "default",
    "rules": {
        PRIVILEGED: {"enforced": True},
        RUN_AS_ROOT: {"enforced": True},
        HOST_PATH: {"enforced": True, "allowedPaths": []},
        RUN_IDENTITY_UNCONFIRMED: {"enforced": True},
    },
}


class InputError(ValueError):
    """输入 JSON 结构不合法。"""


def parse_time(value):
    """解析 ISO 8601 时间（支持结尾 Z）。返回带时区的 datetime。"""
    if not isinstance(value, str) or not value.strip():
        raise InputError("时间字段必须是非空 ISO 8601 字符串，实际为: %r" % (value,))
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InputError("无法解析的时间: %r" % (value,))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _require_dict(obj, what):
    if not isinstance(obj, dict):
        raise InputError("%s 必须是 JSON 对象" % what)
    return obj


def _require_list_of_dicts(obj, what):
    if obj is None:
        return []
    if not isinstance(obj, list) or any(not isinstance(x, dict) for x in obj):
        raise InputError("%s 必须是对象数组" % what)
    return obj


def validate_workload(workload, what="工作负载"):
    _require_dict(workload, what)
    for key in ("containers", "initContainers", "volumes"):
        _require_list_of_dicts(workload.get(key), "%s.%s" % (what, key))
    defaults = workload.get("defaults", {})
    _require_dict(defaults, "%s.defaults" % what)
    if "runAsUser" in defaults and not isinstance(defaults["runAsUser"], int):
        raise InputError("%s.defaults.runAsUser 必须是整数" % what)
    for key in ("containers", "initContainers"):
        for c in workload.get(key) or []:
            if not isinstance(c.get("name"), str) or not c["name"]:
                raise InputError("%s.%s 中每个容器必须有非空 name" % (what, key))
            if not isinstance(c.get("image"), str) or not c["image"]:
                raise InputError("%s 中容器 %r 缺少 image" % (what, c.get("name")))
            sc = c.get("securityContext", {})
            _require_dict(sc, "容器 %r 的 securityContext" % c.get("name"))
            if "runAsUser" in sc and not isinstance(sc["runAsUser"], int):
                raise InputError("容器 %r 的 runAsUser 必须是整数" % c.get("name"))
            _require_list_of_dicts(c.get("volumeMounts"), "容器 %r 的 volumeMounts" % c.get("name"))
    return workload


def merge_baseline(baseline):
    """将团队基线与默认值合并；基线可关闭某条规则或配置放行的宿主机路径。"""
    if baseline is None:
        baseline = {}
    _require_dict(baseline, "团队基线")
    merged = {k: dict(v) for k, v in DEFAULT_BASELINE["rules"].items()}
    rules = baseline.get("rules", {})
    _require_dict(rules, "团队基线.rules")
    for name, cfg in rules.items():
        if name not in merged:
            raise InputError("未知基线规则: %r" % (name,))
        _require_dict(cfg, "基线规则 %r" % name)
        merged[name].update(cfg)
    paths = merged[HOST_PATH].get("allowedPaths") or []
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise InputError("host-path 规则的 allowedPaths 必须是字符串数组")
    merged[HOST_PATH]["allowedPaths"] = paths
    return {"team": baseline.get("team", "default"), "rules": merged}


def normalize_exceptions(exceptions_doc):
    """接受 {"exceptions": [...]} 或裸数组。"""
    if exceptions_doc is None:
        return []
    if isinstance(exceptions_doc, dict):
        exceptions_doc = exceptions_doc.get("exceptions", [])
    if not isinstance(exceptions_doc, list) or any(not isinstance(e, dict) for e in exceptions_doc):
        raise InputError("审批例外必须是对象数组或 {\"exceptions\": [...]}")
    for e in exceptions_doc:
        for field in ("namespace", "workload", "container", "imageDigest", "violation"):
            if not isinstance(e.get(field), str) or not e[field]:
                raise InputError("例外缺少必填字段 %r: %r" % (field, e))
        if e["violation"] not in ALL_VIOLATION_TYPES:
            raise InputError("例外包含未知违规项: %r" % (e["violation"],))
        if e["violation"] == HOST_PATH and not isinstance(e.get("hostPath"), str):
            raise InputError("挂载类例外必须提供 hostPath 字符串: %r" % (e,))
        if "expiresAt" in e:
            parse_time(e["expiresAt"])  # 提前校验格式
    return exceptions_doc


def image_digest(image):
    """从镜像引用中提取摘要；tag 引用（无 @）返回 None。"""
    if isinstance(image, str) and "@" in image:
        return image.split("@", 1)[1]
    return None


def _path_allowed(path, allowed_paths):
    """宿主机路径命中放行清单：精确匹配或位于放行目录之下。"""
    for allowed in allowed_paths:
        prefix = allowed.rstrip("/")
        if path == allowed or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def match_exceptions(exceptions, namespace, workload_name, container_name, digest,
                     violation_type, host_path, now):
    """返回 (status, exception_id)。status ∈ {applied, expired, none}。"""
    expired_id = None
    for exc in exceptions:
        if exc["violation"] != violation_type:
            continue
        if exc["namespace"] != namespace:
            continue
        if exc["workload"] != workload_name:
            continue
        if exc["container"] != container_name:
            continue
        if digest is None or exc["imageDigest"] != digest:
            continue
        if violation_type == HOST_PATH and exc.get("hostPath") != host_path:
            continue
        exc_id = exc.get("id", "<无id>")
        expires = exc.get("expiresAt")
        if expires is not None and parse_time(expires) <= now:
            expired_id = exc_id
            continue
        return "applied", exc_id
    if expired_id is not None:
        return "expired", expired_id
    return "none", None


def evaluate_workload(workload, baseline, exceptions, now, label="candidate"):
    """用同一基线评估一个版本的工作负载，返回违规列表（含例外匹配情况）。"""
    validate_workload(workload, "工作负载(%s)" % label)
    rules = baseline["rules"]
    namespace = workload.get("namespace", "")
    workload_name = workload.get("name", "")
    default_uid = workload.get("defaults", {}).get("runAsUser")
    volumes = {v.get("name"): v for v in workload.get("volumes", []) if isinstance(v, dict)}

    violations = []

    def add(vtype, kind, key, index, container, effective_value, message, host_path=None):
        digest = image_digest(container.get("image"))
        status, exc_id = match_exceptions(
            exceptions, namespace, workload_name, container["name"],
            digest, vtype, host_path, now)
        violations.append({
            "id": "V%03d" % (len(violations) + 1),
            "type": vtype,
            "location": "%s[%d](%s)" % (key, index, container["name"]),
            "containerKind": kind,
            "container": container["name"],
            "image": container.get("image"),
            "imageDigest": digest,
            "effectiveValue": effective_value,
            "hostPath": host_path,
            "message": message,
            "exception": {"status": status, "exceptionId": exc_id},
            "suppressed": status == "applied",
        })

    for kind, key in (("container", "containers"), ("initContainer", "initContainers")):
        for index, c in enumerate(workload.get(key) or []):
            sc = c.get("securityContext", {})
            loc = "%s[%d](%s)" % (key, index, c["name"])

            # 1) 特权模式
            if rules[PRIVILEGED]["enforced"] and sc.get("privileged") is True:
                add(PRIVILEGED, kind, key, index, c, True,
                    "%s 以特权模式运行 (privileged=true)" % loc)

            # 2) 运行身份：容器显式 runAsUser 覆盖工作负载默认值
            if "runAsUser" in sc:
                uid = sc["runAsUser"]
                uid_source = "container"
            elif default_uid is not None:
                uid = default_uid
                uid_source = "workload-default"
            else:
                uid = None
                uid_source = "none"
            if uid is None:
                if rules[RUN_IDENTITY_UNCONFIRMED]["enforced"]:
                    add(RUN_IDENTITY_UNCONFIRMED, kind, key, index, c, None,
                        "%s 未设置 runAsUser（容器与工作负载默认值均缺失），运行身份无法确认" % loc)
            elif uid == 0 and rules[RUN_AS_ROOT]["enforced"]:
                add(RUN_AS_ROOT, kind, key, index, c, 0,
                    "%s 以 root 运行 (runAsUser=0, 来源=%s)" % (loc, uid_source))

            # 3) 宿主机目录挂载
            if rules[HOST_PATH]["enforced"]:
                allowed = rules[HOST_PATH]["allowedPaths"]
                for mount in c.get("volumeMounts") or []:
                    vol = volumes.get(mount.get("name"))
                    if not vol or "hostPath" not in vol:
                        continue
                    hp = vol["hostPath"]
                    path = hp.get("path") if isinstance(hp, dict) else None
                    if not isinstance(path, str) or not path:
                        add(HOST_PATH, kind, key, index, c, path,
                            "%s 挂载的卷 %r 的 hostPath 缺少有效路径" % (loc, mount.get("name")),
                            host_path=path if isinstance(path, str) else None)
                        continue
                    if _path_allowed(path, allowed):
                        continue
                    add(HOST_PATH, kind, key, index, c, path,
                        "%s 挂载宿主机目录 %s (卷 %r)" % (loc, path, mount.get("name")),
                        host_path=path)
    return violations


def violation_key(v):
    """用于跨版本比对的违规身份：类型 + 容器类别 + 容器名 + 宿主机路径。"""
    return (v["type"], v["containerKind"], v["container"], v.get("hostPath"))


def check_release(candidate, baseline=None, exceptions=None, deployed=None, now=None):
    """准入检查主入口。返回可供流水线消费的结果字典。"""
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, str):
        now = parse_time(now)
    merged_baseline = merge_baseline(baseline)
    exception_list = normalize_exceptions(exceptions)

    candidate_violations = evaluate_workload(
        candidate, merged_baseline, exception_list, now, label="candidate")

    deployed_violations = None
    if deployed is not None:
        deployed_violations = evaluate_workload(
            deployed, merged_baseline, exception_list, now, label="deployed")
        deployed_open_keys = {violation_key(v) for v in deployed_violations if not v["suppressed"]}
        for v in candidate_violations:
            v["isNew"] = violation_key(v) not in deployed_open_keys
    else:
        for v in candidate_violations:
            v["isNew"] = None

    open_violations = [v for v in candidate_violations if not v["suppressed"]]
    new_violations = [v for v in open_violations if v["isNew"]]
    decision = "block" if open_violations else "allow"

    result = {
        "decision": decision,
        "reasons": [v["message"] for v in open_violations],
        "evaluatedAt": now.isoformat(),
        "baseline": {"team": merged_baseline["team"], "rules": merged_baseline["rules"]},
        "candidate": {
            "namespace": candidate.get("namespace", ""),
            "workload": candidate.get("name", ""),
            "violations": candidate_violations,
        },
        "summary": {
            "totalViolations": len(candidate_violations),
            "suppressedByException": sum(1 for v in candidate_violations if v["suppressed"]),
            "unsuppressed": len(open_violations),
            "newViolations": len(new_violations) if deployed is not None else None,
        },
    }
    if deployed is not None:
        result["deployed"] = {
            "namespace": deployed.get("namespace", ""),
            "workload": deployed.get("name", ""),
            "violations": deployed_violations,
        }
        result["newViolations"] = new_violations
    return result
