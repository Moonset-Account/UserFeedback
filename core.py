"""SEC-VULN-006: 签名下载对象规范化一致性检查（核心逻辑）。

背景：下载服务按对象编号授权。存储迁移后，签名校验模块与对象定位模块
可能以不同规则理解同一条请求记录。本模块用两套可配置规则分别把一条
已解析的合成记录规范化为 (租户, 对象编号, 版本, 保留查询项)，仅当两套
规则在租户、编号、版本上完全一致时该记录才通过。

分歧报告只包含：记录编号、两套规则版本、导致分歧的字段名称。
"""

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

#: 规范化输出中参与一致性比对的字段（顺序即报告中的字段名顺序）。
COMPARE_FIELDS = ("tenant", "object_no", "version", "query")

#: different handling results for duplicated version fields
DUPLICATE_VERSION_POLICIES = ("first", "last", "reject")

#: handling results for a missing version field
MISSING_VERSION_POLICIES = ("reject", "default")


class RuleError(Exception):
    """某套规则无法规范化记录时抛出，携带导致失败的字段名与原因。"""

    def __init__(self, field_name, reason):
        super().__init__("%s: %s" % (field_name, reason))
        self.field_name = field_name
        self.reason = reason


@dataclass(frozen=True)
class RuleSet:
    """一套可配置的规范化规则。

    name/version:        规则标识与版本（版本会出现在分歧报告中）。
    tenant_aliases:      租户别名 -> 规范租户名 的映射（含旧别名）。
    known_tenants:       规范租户名集合；既不在别名表也不在集合中的租户
                         视为“未解析的租户别名”，按 RuleError 处理。
    duplicate_version:   重复版本字段处理："first" 取首个 / "last" 取末个 /
                         "reject" 拒绝该记录。
    missing_version:     版本缺失处理："reject" 拒绝 / "default" 用默认值。
    default_version:     missing_version == "default" 时使用的版本。
    ignored_query_keys:  可忽略的追踪字段（查询键），支持 fnmatch 通配，
                         如 "trace_id"、"utm_*"；这些键不参与一致性比对。
    """

    name: str
    version: str
    tenant_aliases: dict = field(default_factory=dict)
    known_tenants: tuple = ()
    duplicate_version: str = "reject"
    missing_version: str = "reject"
    default_version: str = "0"
    ignored_query_keys: tuple = ()

    def __post_init__(self):
        if self.duplicate_version not in DUPLICATE_VERSION_POLICIES:
            raise ValueError("unknown duplicate_version policy: %r" % (self.duplicate_version,))
        if self.missing_version not in MISSING_VERSION_POLICIES:
            raise ValueError("unknown missing_version policy: %r" % (self.missing_version,))


def _is_ignored_query_key(rules, key):
    return any(fnmatchcase(key, pattern) for pattern in rules.ignored_query_keys)


def resolve_tenant(rules, raw_tenant):
    """解析租户：别名映射优先，其次规范名，否则报未解析别名。"""
    if raw_tenant is None or raw_tenant == "":
        raise RuleError("tenant", "missing tenant")
    if raw_tenant in rules.tenant_aliases:
        return rules.tenant_aliases[raw_tenant]
    if raw_tenant in rules.known_tenants:
        return raw_tenant
    raise RuleError("tenant", "unresolved tenant alias: %r" % (raw_tenant,))


def resolve_version(rules, raw_version):
    """解析版本：处理缺失与重复字段两种边界。"""
    if raw_version is None:
        if rules.missing_version == "default":
            return rules.default_version
        raise RuleError("version", "missing version")
    if isinstance(raw_version, (list, tuple)):
        # 输入为已解析记录，重复版本字段表示为列表。
        if not raw_version:
            if rules.missing_version == "default":
                return rules.default_version
            raise RuleError("version", "missing version")
        if len(raw_version) == 1:
            return str(raw_version[0])
        if rules.duplicate_version == "first":
            return str(raw_version[0])
        if rules.duplicate_version == "last":
            return str(raw_version[-1])
        raise RuleError("version", "duplicate version fields: %r" % (list(raw_version),))
    return str(raw_version)


def canonicalize(rules, record):
    """按一套规则把记录规范化为可比对的字典。

    记录字段：record_no(来源编号)、tenant、object_no、version、query。
    可忽略的追踪字段在规范化时被丢弃，不参与比对。
    """
    tenant = resolve_tenant(rules, record.get("tenant"))

    object_no = record.get("object_no")
    if object_no is None or object_no == "":
        raise RuleError("object_no", "missing object number")
    object_no = str(object_no)

    version = resolve_version(rules, record.get("version"))

    raw_query = record.get("query") or {}
    query = {
        str(k): str(v)
        for k, v in sorted(raw_query.items(), key=lambda item: str(item[0]))
        if not _is_ignored_query_key(rules, str(k))
    }

    return {
        "tenant": tenant,
        "object_no": object_no,
        "version": version,
        "query": query,
    }


def _canonical_or_error(rules, record):
    try:
        return canonicalize(rules, record), None
    except RuleError as exc:
        return None, exc


def check_record(record, rules_a, rules_b):
    """对单条记录执行两套规则并比对。

    返回 dict：
      record_no        来源编号
      status           "pass" | "divergent" | "both_rejected"
      divergent_fields 导致分歧的字段名列表（status == "divergent" 时非空）
      errors           两套规则各自拒绝记录时的原因（仅供调试，不进报告）
    """
    record_no = record.get("record_no")
    canon_a, err_a = _canonical_or_error(rules_a, record)
    canon_b, err_b = _canonical_or_error(rules_b, record)

    errors = {}
    if err_a is not None:
        errors[rules_a.name] = "%s: %s" % (err_a.field_name, err_a.reason)
    if err_b is not None:
        errors[rules_b.name] = "%s: %s" % (err_b.field_name, err_b.reason)

    if err_a is not None and err_b is not None:
        if err_a.field_name == err_b.field_name:
            # 两套规则在同一字段上一致地拒绝：理解一致，不算分歧。
            status, divergent = "both_rejected", []
        else:
            # 两套规则因不同字段拒绝：分歧字段为两边的拒绝原因字段。
            pair = {err_a.field_name, err_b.field_name}
            divergent = [name for name in COMPARE_FIELDS if name in pair]
            status = "divergent"
    elif err_a is not None or err_b is not None:
        # 仅一侧拒绝：导致分歧的就是该侧无法规范化的字段。
        err = err_a if err_a is not None else err_b
        status, divergent = "divergent", [err.field_name]
    else:
        divergent = [
            name for name in COMPARE_FIELDS if canon_a[name] != canon_b[name]
        ]
        status = "divergent" if divergent else "pass"

    return {
        "record_no": record_no,
        "status": status,
        "divergent_fields": divergent,
        "errors": errors,
    }


def check_records(records, rules_a, rules_b):
    """逐条检查；分歧报告按记录编号排序，与输入顺序无关。"""
    results = [check_record(rec, rules_a, rules_b) for rec in records]
    report = [
        {
            "record_no": r["record_no"],
            "rule_versions": [rules_a.version, rules_b.version],
            "divergent_fields": list(r["divergent_fields"]),
        }
        for r in results
        if r["status"] == "divergent"
    ]
    report.sort(key=lambda entry: str(entry["record_no"]))
    return {"results": results, "report": report}


def ruleset_from_dict(data):
    """从 JSON 字典构造 RuleSet（配置即规则）。"""
    return RuleSet(
        name=str(data["name"]),
        version=str(data["version"]),
        tenant_aliases=dict(data.get("tenant_aliases", {})),
        known_tenants=tuple(data.get("known_tenants", ())),
        duplicate_version=str(data.get("duplicate_version", "reject")),
        missing_version=str(data.get("missing_version", "reject")),
        default_version=str(data.get("default_version", "0")),
        ignored_query_keys=tuple(data.get("ignored_query_keys", ())),
    )
