"""GraphQL 解析器授权覆盖检查核心逻辑。

离线输入:
  schema     -- 精简 schema 表 (类型、字段、接口、可能实现类型、敏感标记)
  resolvers  -- 具体解析器授权契约 + 接口->实现 继承证据
  document   -- 已解析的字段选择集与命名片段

输出三类结论:
  covered       可证实已覆盖 (契约存在且 checks 非空)
  missing       缺少授权检查 (契约存在但 checks 为空) -> CI 失败
  manual_review 需人工核对 (解析器元数据缺失或继承顺序证据缺失)

片段循环与缺失的解析器元数据在 diagnostics 中分别报告。
"""

MISSING = "missing"
COVERED = "covered"
MANUAL_REVIEW = "manual_review"


class InputError(Exception):
    """离线输入结构不合法。"""


def _possible_types(types, type_name):
    tdef = types.get(type_name)
    if tdef is None:
        raise InputError("unknown type: %r" % type_name)
    if tdef.get("kind") == "interface":
        return list(tdef.get("possible_types", []))
    return [type_name]


def _spread_names(selections):
    names = []
    for sel in selections:
        kind = sel.get("kind", "field")
        if kind == "fragment_spread":
            names.append(sel["name"])
        elif kind == "inline_fragment":
            names.extend(_spread_names(sel.get("selections", [])))
        elif kind == "field":
            names.extend(_spread_names(sel.get("selections", [])))
    return names


class Checker:
    def __init__(self, schema, resolvers, document):
        self.types = schema.get("types", {})
        self.contracts = resolvers.get("contracts", {})
        self.inheritance = resolvers.get("inheritance", [])
        self.operations = document.get("operations", [])
        self.fragments = document.get("fragments", {})
        self.findings = []
        self.diagnostics = {
            "fragment_cycles": [],
            "missing_resolver_metadata": [],
            "unknown_fields": [],
        }

    # ------------------------------------------------------------------ API
    def run(self):
        self._detect_fragment_cycles()
        for op in self.operations:
            name = op.get("name", "<anonymous>")
            root = op.get("root_type")
            if root not in self.types:
                raise InputError("operation %r has unknown root_type %r" % (name, root))
            self._walk(root, op.get("selections", []), name, name, frozenset(), ())
        return self.report()

    def report(self):
        grouped = {MISSING: [], COVERED: [], MANUAL_REVIEW: []}
        for f in self.findings:
            grouped[f["status"]].append(f)
        return {
            "summary": {k: len(v) for k, v in grouped.items()},
            MISSING: grouped[MISSING],
            COVERED: grouped[COVERED],
            MANUAL_REVIEW: grouped[MANUAL_REVIEW],
            "diagnostics": self.diagnostics,
        }

    # -------------------------------------------------------------- walking
    def _walk(self, parent_type, selections, op_name, path, spread_stack, conditions):
        tdef = self.types.get(parent_type)
        if tdef is None:
            raise InputError("unknown type: %r" % parent_type)
        fields = tdef.get("fields", {})
        for sel in selections:
            kind = sel.get("kind", "field")
            if kind == "field":
                name = sel["name"]
                alias = sel.get("alias")
                fdef = fields.get(name)
                if fdef is None:
                    self.diagnostics["unknown_fields"].append({
                        "operation": op_name, "path": path,
                        "parent_type": parent_type, "field": name,
                    })
                    continue
                child_path = "%s.%s" % (path, alias or name)
                if fdef.get("sensitive"):
                    self._evaluate(op_name, child_path, parent_type, name, alias, conditions)
                sub = sel.get("selections")
                if sub:
                    # 进入子选择集后, 外层片段类型条件不再适用
                    self._walk(fdef["type"], sub, op_name, child_path, spread_stack, ())
            elif kind == "fragment_spread":
                fname = sel["name"]
                if fname in spread_stack:
                    continue  # 循环已在 diagnostics 报告, 终止递归
                frag = self.fragments.get(fname)
                if frag is None:
                    raise InputError("unknown fragment: %r" % fname)
                conds = conditions + ((frag["type_condition"],) if frag.get("type_condition") else ())
                self._walk(parent_type, frag.get("selections", []), op_name, path,
                           spread_stack | {fname}, conds)
            elif kind == "inline_fragment":
                conds = conditions + ((sel["type_condition"],) if sel.get("type_condition") else ())
                self._walk(parent_type, sel.get("selections", []), op_name, path,
                           spread_stack, conds)
            else:
                raise InputError("unknown selection kind: %r" % kind)

    # ----------------------------------------------------------- evaluation
    def _evaluate(self, op_name, path, parent_type, field_name, alias, conditions):
        applicable = _possible_types(self.types, parent_type)
        for cond in conditions:
            cond_types = set(_possible_types(self.types, cond))
            applicable = [t for t in applicable if t in cond_types]
        details = [self._contract_for(t, field_name, op_name, path) for t in applicable]
        if any(d["status"] == MISSING for d in details):
            status = MISSING
        elif any(d["status"] == MANUAL_REVIEW for d in details):
            status = MANUAL_REVIEW
        else:
            status = COVERED
        self.findings.append({
            "operation": op_name,
            "path": path,
            "parent_type": parent_type,
            "field": field_name,
            "alias": alias,
            "status": status,
            "applicable": details,
        })

    def _contract_for(self, ctype, fname, op_name, path):
        key = "%s.%s" % (ctype, fname)
        if key in self.contracts:
            checks = list(self.contracts[key].get("checks", []))
            return {
                "type": ctype, "via": "direct", "contract": key,
                "checks": checks,
                "status": COVERED if checks else MISSING,
            }
        # 按声明的接口顺序查找可继承的接口契约
        candidates = []
        for iface in self.types.get(ctype, {}).get("interfaces", []):
            idef = self.types.get(iface, {})
            if fname in idef.get("fields", {}) and ("%s.%s" % (iface, fname)) in self.contracts:
                candidates.append(iface)
        if candidates:
            ev = self._inheritance_evidence(candidates, fname, ctype)
            if ev is None or not ev.get("evidence"):
                return {
                    "type": ctype, "via": "inheritance", "contract": None,
                    "candidate_interfaces": ["%s.%s" % (i, fname) for i in candidates],
                    "checks": [], "status": MANUAL_REVIEW,
                    "reason": "inheritance order evidence missing",
                }
            src = ev.get("interface_field", "%s.%s" % (candidates[0], fname))
            checks = list(self.contracts[src].get("checks", []))
            return {
                "type": ctype, "via": "inherited", "contract": src,
                "checks": checks, "evidence": ev.get("evidence"),
                "status": COVERED if checks else MISSING,
            }
        self.diagnostics["missing_resolver_metadata"].append({
            "type": ctype, "field": fname, "operation": op_name, "path": path,
        })
        return {
            "type": ctype, "via": "none", "contract": None, "checks": [],
            "status": MANUAL_REVIEW, "reason": "resolver metadata missing",
        }

    def _inheritance_evidence(self, candidates, fname, ctype):
        wanted = {"%s.%s" % (i, fname) for i in candidates}
        for entry in self.inheritance:
            if entry.get("implementation") == ctype and entry.get("interface_field") in wanted:
                return entry
        return None

    # ------------------------------------------------------------ diagnostics
    def _detect_fragment_cycles(self):
        graph = {name: _spread_names(frag.get("selections", []))
                 for name, frag in self.fragments.items()}
        color = {n: 0 for n in graph}  # 0=white 1=gray 2=black
        stack = []
        cycles = []

        def dfs(node):
            color[node] = 1
            stack.append(node)
            for nxt in graph.get(node, []):
                if nxt not in graph:
                    continue
                if color[nxt] == 1:
                    cycles.append(stack[stack.index(nxt):] + [nxt])
                elif color[nxt] == 0:
                    dfs(nxt)
            stack.pop()
            color[node] = 2

        for n in graph:
            if color[n] == 0:
                dfs(n)
        self.diagnostics["fragment_cycles"] = cycles
