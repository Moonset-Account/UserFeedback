"""分流规则生成与试算查询。

语义(来自需求):
- 输出一:按分支生成生效的域名分流规则(分支专属记录覆盖同后缀的全局记录)。
- 输出二:试算入口 query(branch, name),返回该查询的去向。
- 优先最长匹配后缀(按标签数)。
- 内部规则指向不可用的解析器(停用或缺失)时,返回内部解析失败,
  且查询不得转给公共解析器(forwarded_to_public 恒为 False)。
"""

from .normalize import normalize_name, split_labels, suffix_matches


class UnknownBranchError(KeyError):
    """查询或生成时引用了配置中不存在的分支。"""


class Engine(object):
    def __init__(self, config):
        """config 必须是 splitdns.config.validate_config 的返回值。"""
        self._config = config
        self._branches = {b["id"]: b for b in config["branches"]}
        self._resolvers = config["resolvers"]
        self._rules = config["suffix_rules"]
        self._default_resolver = config["default_resolver"]

    # ------------------------------------------------------------------ #
    # 输出一:按分支生成生效规则
    # ------------------------------------------------------------------ #
    def effective_rules(self, branch_id):
        """返回某分支的生效规则列表。

        同一(规范化)后缀下,分支专属记录覆盖全局记录;
        结果按后缀标签数从多到少排序(即匹配优先级从高到低)。
        """
        self._require_branch(branch_id)
        global_rules = {}
        branch_rules = {}
        for rule in self._rules:
            if rule["branch"] is None:
                global_rules[rule["suffix"]] = rule
            elif rule["branch"] == branch_id:
                branch_rules[rule["suffix"]] = rule
        merged = dict(global_rules)
        merged.update(branch_rules)  # 分支记录覆盖同后缀全局记录
        effective = []
        for suffix, rule in merged.items():
            effective.append({
                "suffix": suffix,
                "resolver": rule["resolver"],
                "source": "branch" if suffix in branch_rules else "global",
                "label_count": len(split_labels(suffix)),
            })
        effective.sort(key=lambda r: (-r["label_count"], r["suffix"]))
        return effective

    def generate(self):
        """为配置中所有分支生成生效规则,返回 {branch_id: [rules]}。"""
        return {b["id"]: self.effective_rules(b["id"])
                for b in self._config["branches"]}

    # ------------------------------------------------------------------ #
    # 输出二:试算查询
    # ------------------------------------------------------------------ #
    def query(self, branch_id, name):
        """试算一次查询的去向,返回结构化结果 dict。

        status 取值:
          - "ok":               命中可用解析器(内部或默认出口)
          - "internal_failure": 内部规则指向不可用解析器,未转公共
          - "resolver_unavailable": 默认出口解析器不可用
          - "no_match":         未命中任何后缀且未配置默认出口
        """
        self._require_branch(branch_id)
        normalized = normalize_name(name)
        rule = self._longest_match(branch_id, normalized)
        if rule is None:
            return self._default_egress(branch_id, name, normalized)
        resolver = self._resolvers.get(rule["resolver"])
        available = resolver is not None and resolver["enabled"]
        if not available:
            # 内部规则指向不可用解析器:内部解析失败,绝不回落公共。
            return {
                "branch": branch_id,
                "query": name,
                "normalized": normalized,
                "status": "internal_failure",
                "matched_suffix": rule["suffix"],
                "rule_source": rule["source"],
                "resolver": self._resolver_view(rule["resolver"], resolver),
                "forwarded_to_public": False,
                "detail": "internal resolver %r is unavailable; "
                          "query must not fall through to a public resolver"
                          % rule["resolver"],
            }
        return {
            "branch": branch_id,
            "query": name,
            "normalized": normalized,
            "status": "ok",
            "matched_suffix": rule["suffix"],
            "rule_source": rule["source"],
            "resolver": self._resolver_view(rule["resolver"], resolver),
            "forwarded_to_public": resolver["type"] == "public",
            "detail": "resolved via %s resolver %r"
                      % (resolver["type"], resolver["id"]),
        }

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _require_branch(self, branch_id):
        if branch_id not in self._branches:
            raise UnknownBranchError(branch_id)

    def _longest_match(self, branch_id, normalized):
        """在生效规则中找最长匹配后缀;同后缀分支记录已覆盖全局记录。"""
        best = None
        for rule in self.effective_rules(branch_id):
            if suffix_matches(normalized, rule["suffix"]):
                if (best is None
                        or rule["label_count"] > best["label_count"]):
                    best = rule
        return best

    def _default_egress(self, branch_id, name, normalized):
        """未命中内部后缀:走默认出口。"""
        rid = self._default_resolver
        if rid is None:
            return {
                "branch": branch_id,
                "query": name,
                "normalized": normalized,
                "status": "no_match",
                "matched_suffix": None,
                "rule_source": None,
                "resolver": None,
                "forwarded_to_public": False,
                "detail": "no suffix rule matched and no default egress "
                          "resolver is configured",
            }
        resolver = self._resolvers.get(rid)
        if resolver is None or not resolver["enabled"]:
            return {
                "branch": branch_id,
                "query": name,
                "normalized": normalized,
                "status": "resolver_unavailable",
                "matched_suffix": None,
                "rule_source": None,
                "resolver": self._resolver_view(rid, resolver),
                "forwarded_to_public": False,
                "detail": "default egress resolver %r is unavailable" % rid,
            }
        return {
            "branch": branch_id,
            "query": name,
            "normalized": normalized,
            "status": "ok",
            "matched_suffix": None,
            "rule_source": "default",
            "resolver": self._resolver_view(rid, resolver),
            "forwarded_to_public": resolver["type"] == "public",
            "detail": "no internal suffix matched; sent to default egress "
                      "resolver %r" % rid,
        }

    @staticmethod
    def _resolver_view(resolver_id, resolver):
        if resolver is None:
            return {"id": resolver_id, "available": False}
        return {
            "id": resolver["id"],
            "type": resolver["type"],
            "address": resolver["address"],
            "available": resolver["enabled"],
        }
