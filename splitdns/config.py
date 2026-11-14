"""配置加载与校验。

输入配置(JSON)包含三类清单:
- branches:       分支机构清单,每项 {"id": "bj-01", "name": 可选}
- resolvers:      受管解析器列表,每项
                  {"id": "internal-corp", "type": "internal"|"public",
                   "address": "10.1.0.53", "enabled": true|false}
- suffix_rules:   内部域名后缀表,每项
                  {"suffix": "corp.example.com", "resolver": "internal-corp",
                   "branch": 可选分支 id}
                  不带 branch 的是全局记录;带 branch 的是分支专属记录,
                  同一(规范化后)后缀下分支记录覆盖全局记录。
- default_resolver: 可选,未命中任何内部后缀时的默认出口解析器 id。
"""

import json

from .normalize import normalize_name


class ConfigError(ValueError):
    """配置内容不合法。"""


def _require(cond, message):
    if not cond:
        raise ConfigError(message)


def _validate_branches(raw):
    _require(isinstance(raw, list) and raw, "branches must be a non-empty list")
    seen = set()
    branches = []
    for i, item in enumerate(raw):
        _require(isinstance(item, dict), "branches[%d] must be an object" % i)
        bid = item.get("id")
        _require(isinstance(bid, str) and bid.strip(),
                 "branches[%d].id must be a non-empty string" % i)
        bid = bid.strip()
        _require(bid not in seen, "duplicate branch id: %s" % bid)
        seen.add(bid)
        branches.append({"id": bid, "name": str(item.get("name", bid))})
    return branches


def _validate_resolvers(raw):
    _require(isinstance(raw, list) and raw, "resolvers must be a non-empty list")
    seen = set()
    resolvers = {}
    for i, item in enumerate(raw):
        _require(isinstance(item, dict), "resolvers[%d] must be an object" % i)
        rid = item.get("id")
        _require(isinstance(rid, str) and rid.strip(),
                 "resolvers[%d].id must be a non-empty string" % i)
        rid = rid.strip()
        _require(rid not in seen, "duplicate resolver id: %s" % rid)
        seen.add(rid)
        rtype = item.get("type")
        _require(rtype in ("internal", "public"),
                 "resolvers[%d].type must be 'internal' or 'public'" % i)
        enabled = item.get("enabled", True)
        _require(isinstance(enabled, bool),
                 "resolvers[%d].enabled must be a boolean" % i)
        resolvers[rid] = {
            "id": rid,
            "type": rtype,
            "address": str(item.get("address", "")),
            "enabled": enabled,
        }
    return resolvers


def _validate_suffix_rules(raw, branch_ids, resolver_ids):
    _require(isinstance(raw, list), "suffix_rules must be a list")
    rules = []
    for i, item in enumerate(raw):
        _require(isinstance(item, dict), "suffix_rules[%d] must be an object" % i)
        suffix = item.get("suffix")
        _require(isinstance(suffix, str) and normalize_name(suffix) != "",
                 "suffix_rules[%d].suffix must be a non-empty domain" % i)
        resolver = item.get("resolver")
        _require(isinstance(resolver, str) and resolver in resolver_ids,
                 "suffix_rules[%d].resolver %r is not a known resolver id"
                 % (i, resolver))
        branch = item.get("branch")
        if branch is not None:
            _require(isinstance(branch, str) and branch in branch_ids,
                     "suffix_rules[%d].branch %r is not a known branch id"
                     % (i, branch))
        rules.append({
            "suffix": normalize_name(suffix),
            "resolver": resolver,
            "branch": branch,
        })
    return rules


def validate_config(raw):
    """校验原始 dict,返回规范化后的配置 dict。不合法时抛 ConfigError。"""
    _require(isinstance(raw, dict), "config root must be an object")
    branches = _validate_branches(raw.get("branches"))
    resolvers = _validate_resolvers(raw.get("resolvers"))
    branch_ids = {b["id"] for b in branches}
    rules = _validate_suffix_rules(raw.get("suffix_rules", []),
                                   branch_ids, set(resolvers))
    default_resolver = raw.get("default_resolver")
    if default_resolver is not None:
        _require(isinstance(default_resolver, str)
                 and default_resolver in resolvers,
                 "default_resolver %r is not a known resolver id"
                 % (default_resolver,))
    return {
        "branches": branches,
        "resolvers": resolvers,
        "suffix_rules": rules,
        "default_resolver": default_resolver,
    }


def load_config(path):
    """从 JSON 文件加载并校验配置。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return validate_config(raw)
