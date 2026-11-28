"""secure_export —— 财务工作簿脱敏导出模块（可嵌入导出器）。

只实现防御性的导出过滤，不生成、不执行任何工作簿之外的载荷：

* 接收单元格值、限定的算术/条件公式、命名范围、每格密级以及
  允许公开的结果清单（导出策略）。
* 获准公开数值的格子固化为常量（公式被移除，内部引用随之消失）。
* 获准保留公式的格子，只有其全部引用（含 IF 判定项、命名范围
  指向的格子）也获准进入导出物时才保留公式，否则回退为常量
  （若获准公开数值）或放弃导出。
* 导出物中重建名称表：仅保留被保留公式实际使用、且目标格子本身
  获准导出的命名范围；隐藏页与未使用的命名范围一律不进入导出物。
* 检测循环引用：依赖环的导出结果被列入 ``cycle_blocked`` 报告，
  其余格子照常生成。

限定的公式语法（有意保持小而可审计）：

* 数字、字符串（双引号，``""`` 转义）、单元格引用
  （``A1``、``$A$1``、``Sheet2!A1``、``'My Sheet'!A1``）、命名范围；
* 算术 ``+ - * /``、括号、一元正负号；
* 比较 ``= <> < > <= >=``；
* 条件函数 ``IF(条件, 真值, 假值)``，可嵌套。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = [
    "CellSpec",
    "Workbook",
    "ExportPolicy",
    "ExportedCell",
    "ExportResult",
    "FormulaError",
    "EvaluationError",
    "export_workbook",
]

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class FormulaError(ValueError):
    """公式或命名范围目标无法解析。"""


class EvaluationError(Exception):
    """公式求值失败（除零、类型不匹配、引用缺失等）。"""


# ---------------------------------------------------------------------------
# 公开数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellSpec:
    """一个单元格的描述。

    ``sheet``/``address`` 定位；``value`` 为静态值或缓存值；
    ``formula`` 为限定语法公式（可带前导 ``=``）；``level`` 为密级。
    """

    sheet: str
    address: str
    value: Any = None
    formula: str | None = None
    level: str = "public"


@dataclass(frozen=True)
class Workbook:
    """待导出的工作簿快照。

    ``named_ranges`` 形如 ``{"Rate": "Sheet1!A1"}`` 或
    ``{"Pair": "Sheet1!A1:A2"}``；``hidden_sheets`` 为隐藏页名集合。
    """

    cells: tuple[CellSpec, ...]
    named_ranges: dict[str, str] = field(default_factory=dict)
    hidden_sheets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExportPolicy:
    """导出策略。

    ``publish_values``：获准公开数值、可固化为常量的格子引用；
    ``keep_formulas``：获准保留公式的格子引用（引用也获准才生效）；
    ``allowed_levels``：允许进入导出物的密级集合。
    引用写法 ``"Sheet1!A1"``，缺省工作表时由 ``default_sheet`` 补齐。
    """

    publish_values: frozenset[str] = frozenset()
    keep_formulas: frozenset[str] = frozenset()
    allowed_levels: frozenset[str] = frozenset({"public"})


@dataclass
class ExportedCell:
    """导出物中的一个格子：``formula`` 为 ``None`` 表示已固化为常量。"""

    ref: str
    value: Any
    formula: str | None = None


@dataclass
class ExportResult:
    """导出结果。

    ``cells``：成功导出的格子（规范引用 -> ExportedCell）；
    ``named_ranges``：重建后的名称表（仅含被保留公式实际使用、
    且目标获准导出的名称）；
    ``cycles``：发现的全部循环（每个环为规范引用列表）；
    ``cycle_blocked``：因依赖环而被阻断的导出结果 ->
    其依赖的环列表；
    ``omitted``：策略清单中未能导出的格子 -> 原因；
    ``errors``：求值失败的格子 -> 错误信息。
    """

    cells: dict[str, ExportedCell] = field(default_factory=dict)
    named_ranges: dict[str, str] = field(default_factory=dict)
    cycles: list[list[str]] = field(default_factory=list)
    cycle_blocked: dict[str, list[list[str]]] = field(default_factory=dict)
    omitted: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 引用与地址工具
# ---------------------------------------------------------------------------

_REF_TOKEN_RE = re.compile(
    r"^(?:(?P<sheet>'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
    r"(?P<addr>\$?[A-Za-z]{1,3}\$?\d+)$"
)

_NAME_TARGET_RE = re.compile(
    r"^(?:(?P<sheet>'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
    r"(?P<a>\$?[A-Za-z]{1,3}\$?\d+)"
    r"(?::(?P<b>\$?[A-Za-z]{1,3}\$?\d+))?$"
)

_ADDR_RE = re.compile(r"^([A-Z]+)([0-9]+)$")

_MAX_RANGE_CELLS = 4096


def _canon_addr(addr: str) -> str:
    return addr.replace("$", "").upper()


def _canon_ref(key: tuple[str, str]) -> str:
    return f"{key[0]}!{key[1]}"


def _unquote_sheet(sheet: str) -> str:
    return sheet[1:-1] if sheet.startswith("'") else sheet


def _parse_ref(text: str, default_sheet: str | None) -> tuple[str, str]:
    """把 ``Sheet!A1`` / ``A1`` 形式的引用解析为 (sheet, ADDR) 键。"""
    m = _REF_TOKEN_RE.match(text.strip())
    if not m:
        raise ValueError(f"无效的单元格引用: {text!r}")
    sheet = m.group("sheet")
    if sheet is None:
        if default_sheet is None:
            raise ValueError(f"引用缺少工作表名且未提供 default_sheet: {text!r}")
        sheet = default_sheet
    else:
        sheet = _unquote_sheet(sheet)
    return sheet, _canon_addr(m.group("addr"))


def _split_addr(addr: str) -> tuple[str, int]:
    m = _ADDR_RE.match(addr)
    if not m:
        raise FormulaError(f"无效的单元格地址: {addr!r}")
    return m.group(1), int(m.group(2))


def _col_to_idx(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _idx_to_col(idx: int) -> str:
    out = []
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


def _expand_range(a: str, b: str) -> list[str]:
    c1, r1 = _split_addr(a)
    c2, r2 = _split_addr(b)
    ci1, ci2 = sorted((_col_to_idx(c1), _col_to_idx(c2)))
    ri1, ri2 = sorted((r1, r2))
    if (ci2 - ci1 + 1) * (ri2 - ri1 + 1) > _MAX_RANGE_CELLS:
        raise FormulaError(f"命名范围过大（超过 {_MAX_RANGE_CELLS} 格）: {a}:{b}")
    return [
        f"{_idx_to_col(ci)}{ri}" for ci in range(ci1, ci2 + 1) for ri in range(ri1, ri2 + 1)
    ]


def _parse_name_target(
    text: str, default_sheet: str | None
) -> tuple[frozenset[tuple[str, str]], str]:
    """解析命名范围目标，返回 (目标格子键集合, 规范字符串)。"""
    m = _NAME_TARGET_RE.match(text.strip())
    if not m:
        raise FormulaError(f"无效的命名范围目标: {text!r}")
    sheet = m.group("sheet")
    if sheet is None:
        if default_sheet is None:
            raise FormulaError(f"命名范围缺少工作表名且未提供 default_sheet: {text!r}")
        sheet = default_sheet
    else:
        sheet = _unquote_sheet(sheet)
    a = _canon_addr(m.group("a"))
    b = _canon_addr(m.group("b")) if m.group("b") else None
    if b is None:
        return frozenset({(sheet, a)}), f"{sheet}!{a}"
    members = _expand_range(a, b)
    return frozenset((sheet, addr) for addr in members), f"{sheet}!{a}:{b}"


# ---------------------------------------------------------------------------
# 公式词法与语法
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<SPACE>\s+)
    | (?P<NUMBER>\d+(?:\.\d+)?)
    | (?P<STRING>"(?:[^"]|"")*")
    | (?P<SHEETREF>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_.]*)!\$?[A-Za-z]{1,3}\$?\d+)
    | (?P<CELLREF>\$?[A-Za-z]{1,3}\$?\d+)
    | (?P<NAME>[A-Za-z_][A-Za-z0-9_.]*)
    | (?P<OP><>|<=|>=|[-+*/(),=<>])
    | (?P<MISMATCH>[\s\S])
    """,
    re.VERBOSE,
)

_COMPARISON_OPS = ("=", "<>", "<", ">", "<=", ">=")


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "SPACE":
            continue
        if kind == "MISMATCH":
            raise FormulaError(f"公式含无法识别的字符: {m.group()!r}")
        tokens.append((kind, m.group()))
    return tokens


class _Parser:
    """限定语法的递归下降解析器，产出元组 AST。"""

    def __init__(self, tokens: list[tuple[str, str]], default_sheet: str):
        self._tokens = tokens
        self._pos = 0
        self._default_sheet = default_sheet

    def _peek(self) -> tuple[str | None, str | None]:
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None, None

    def _advance(self) -> tuple[str, str]:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect_op(self, text: str) -> None:
        kind, got = self._peek()
        if kind != "OP" or got != text:
            raise FormulaError(f"期望 {text!r}，实际为 {got!r}")
        self._advance()

    def parse(self) -> tuple:
        if not self._tokens:
            raise FormulaError("空公式")
        node = self._comparison()
        kind, got = self._peek()
        if kind is not None:
            raise FormulaError(f"公式末尾有多余内容: {got!r}")
        return node

    def _comparison(self) -> tuple:
        left = self._additive()
        kind, text = self._peek()
        if kind == "OP" and text in _COMPARISON_OPS:
            self._advance()
            right = self._additive()
            return ("cmp", text, left, right)
        return left

    def _additive(self) -> tuple:
        node = self._multiplicative()
        while True:
            kind, text = self._peek()
            if kind == "OP" and text in ("+", "-"):
                self._advance()
                node = ("bin", text, node, self._multiplicative())
            else:
                return node

    def _multiplicative(self) -> tuple:
        node = self._unary()
        while True:
            kind, text = self._peek()
            if kind == "OP" and text in ("*", "/"):
                self._advance()
                node = ("bin", text, node, self._unary())
            else:
                return node

    def _unary(self) -> tuple:
        kind, text = self._peek()
        if kind == "OP" and text == "-":
            self._advance()
            return ("neg", self._unary())
        if kind == "OP" and text == "+":
            self._advance()
            return self._unary()
        return self._primary()

    def _primary(self) -> tuple:
        kind, text = self._peek()
        if kind is None:
            raise FormulaError("公式意外结束")
        if kind == "NUMBER":
            self._advance()
            return ("num", float(text) if "." in text else int(text))
        if kind == "STRING":
            self._advance()
            return ("str", text[1:-1].replace('""', '"'))
        if kind in ("CELLREF", "SHEETREF"):
            self._advance()
            sheet, addr = _parse_ref(text, self._default_sheet)
            return ("ref", sheet, addr)
        if kind == "NAME":
            self._advance()
            if text.upper() == "IF":
                self._expect_op("(")
                cond = self._comparison()
                self._expect_op(",")
                when_true = self._comparison()
                self._expect_op(",")
                when_false = self._comparison()
                self._expect_op(")")
                return ("if", cond, when_true, when_false)
            next_kind, next_text = self._peek()
            if next_kind == "OP" and next_text == "(":
                raise FormulaError(f"不支持的函数（仅支持 IF）: {text!r}")
            return ("name", text)
        if kind == "OP" and text == "(":
            self._advance()
            node = self._comparison()
            self._expect_op(")")
            return node
        raise FormulaError(f"意外的记号: {text!r}")


def parse_formula(text: str, default_sheet: str) -> tuple:
    """解析限定语法公式为 AST（公开以便测试与审计）。"""
    stripped = text.strip()
    if stripped.startswith("="):
        stripped = stripped[1:]
    return _Parser(_tokenize(stripped), default_sheet).parse()


def _ast_deps(node: tuple, refs: set, names: set) -> None:
    """收集 AST 的静态依赖；IF 的判定项与两个分支全部计入。"""
    tag = node[0]
    if tag == "ref":
        refs.add((node[1], node[2]))
        return
    if tag == "name":
        names.add(node[1])
        return
    for child in node[1:]:
        if isinstance(child, tuple):
            _ast_deps(child, refs, names)


# ---------------------------------------------------------------------------
# 公式求值
# ---------------------------------------------------------------------------


def _require_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{context} 需要数值，实际为 {value!r}")
    return value


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return len(value) > 0
    raise EvaluationError(f"无法判定真假的值: {value!r}")


def _compare(op: str, left: Any, right: Any) -> bool:
    both_num = (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    )
    both_str = isinstance(left, str) and isinstance(right, str)
    both_bool = isinstance(left, bool) and isinstance(right, bool)
    if not (both_num or both_str or both_bool):
        raise EvaluationError(f"比较运算对象类型不匹配: {left!r} 与 {right!r}")
    if both_bool and op not in ("=", "<>"):
        raise EvaluationError("布尔值仅支持 = 与 <> 比较")
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    return left >= right


def _eval_ast(node: tuple, get) -> Any:
    tag = node[0]
    if tag in ("num", "str"):
        return node[1]
    if tag == "ref":
        return get(("ref", node[1], node[2]))
    if tag == "name":
        return get(("name", node[1]))
    if tag == "neg":
        return -_require_number(_eval_ast(node[1], get), "一元负号")
    if tag == "bin":
        op = node[1]
        left = _require_number(_eval_ast(node[2], get), f"运算符 {op}")
        right = _require_number(_eval_ast(node[3], get), f"运算符 {op}")
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if right == 0:
            raise EvaluationError("除数为零")
        return left / right
    if tag == "cmp":
        return _compare(node[1], _eval_ast(node[2], get), _eval_ast(node[3], get))
    if tag == "if":
        branch = node[2] if _truthy(_eval_ast(node[1], get)) else node[3]
        return _eval_ast(branch, get)
    raise EvaluationError(f"未知的 AST 节点: {tag!r}")


# ---------------------------------------------------------------------------
# 循环检测（Kosaraju，迭代实现）
# ---------------------------------------------------------------------------


def _find_cycles(
    nodes: list[tuple[str, str]], edges: dict[tuple[str, str], set[tuple[str, str]]]
) -> list[list[tuple[str, str]]]:
    visited: set = set()
    order: list = []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(edges.get(start, ()))))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    reverse: dict = {n: set() for n in nodes}
    for src, targets in edges.items():
        for tgt in targets:
            reverse.setdefault(tgt, set()).add(src)

    comp_of: dict = {}
    for start in reversed(order):
        if start in comp_of:
            continue
        comp_id = len(comp_of)
        comp_of[start] = comp_id
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in reverse.get(node, ()):
                if nxt not in comp_of:
                    comp_of[nxt] = comp_id
                    stack.append(nxt)

    members_of: dict[int, list] = {}
    for node, comp_id in comp_of.items():
        members_of.setdefault(comp_id, []).append(node)

    cycles = []
    for members in members_of.values():
        if len(members) > 1 or members[0] in edges.get(members[0], set()):
            cycles.append(sorted(members))
    cycles.sort()
    return cycles


# ---------------------------------------------------------------------------
# 内部单元格记录
# ---------------------------------------------------------------------------


class _CellInfo:
    __slots__ = ("spec", "key", "ast", "refs", "names", "dep_keys", "unknown_names")

    def __init__(self, spec: CellSpec, key: tuple[str, str]):
        self.spec = spec
        self.key = key
        self.ast: tuple | None = None
        self.refs: set = set()
        self.names: set = set()
        self.dep_keys: set = set()
        self.unknown_names: set = set()


# ---------------------------------------------------------------------------
# 主导出函数
# ---------------------------------------------------------------------------


def export_workbook(
    workbook: Workbook, policy: ExportPolicy, default_sheet: str | None = None
) -> ExportResult:
    """按策略对工作簿做脱敏导出，返回 :class:`ExportResult`。

    规则（全部为防御性判断）：

    1. 隐藏页上的格子、密级不在 ``allowed_levels`` 的格子一律不导出；
    2. 在 ``publish_values`` 中的格子固化为常量（公式被移除）；
    3. 在 ``keep_formulas`` 中的格子，仅当其全部依赖（含 IF 判定项、
       命名范围目标）也能进入导出物时保留公式，否则回退为常量
       （若同时获准公开数值）或放弃导出；
    4. 名称表只保留被保留公式实际使用、且目标格子均获准导出的名称；
    5. 依赖循环的导出结果列入 ``cycle_blocked``，其余格子照常生成。
    """
    # 1) 索引单元格并解析公式
    cells: dict[tuple[str, str], _CellInfo] = {}
    for spec in workbook.cells:
        key = (spec.sheet, _canon_addr(spec.address))
        if key in cells:
            raise ValueError(f"重复单元格: {_canon_ref(key)}")
        cells[key] = _CellInfo(spec, key)

    for info in cells.values():
        if info.spec.formula:
            # 公式内缺省工作表的引用按所在页解析（与表格软件一致）
            info.ast = parse_formula(info.spec.formula, info.key[0])
            _ast_deps(info.ast, info.refs, info.names)

    # 2) 解析命名范围
    name_targets: dict[str, tuple[frozenset, str]] = {}
    for name, target in workbook.named_ranges.items():
        name_targets[name] = _parse_name_target(target, default_sheet)

    for info in cells.values():
        info.unknown_names = {n for n in info.names if n not in name_targets}
        info.dep_keys = set(info.refs)
        for name in info.names:
            if name in name_targets:
                info.dep_keys |= set(name_targets[name][0])

    # 3) 依赖图与循环检测
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {
        key: {d for d in info.dep_keys if d in cells} for key, info in cells.items()
    }
    cycles = _find_cycles(list(cells), edges)
    cycle_member: dict[tuple[str, str], int] = {}
    for idx, cycle in enumerate(cycles):
        for member in cycle:
            cycle_member[member] = idx

    # tainted：能到达任一环的所有节点（含环成员自身）
    reverse_edges: dict[tuple[str, str], set[tuple[str, str]]] = {k: set() for k in cells}
    for src, targets in edges.items():
        for tgt in targets:
            reverse_edges[tgt].add(src)
    tainted: set[tuple[str, str]] = set()
    stack = list(cycle_member)
    while stack:
        node = stack.pop()
        if node in tainted:
            continue
        tainted.add(node)
        stack.extend(reverse_edges.get(node, ()))

    # 4) 规范化策略清单
    publish = {_parse_ref(r, default_sheet) for r in policy.publish_values}
    keep = {_parse_ref(r, default_sheet) for r in policy.keep_formulas}

    # 5) 逐格裁定：'formula' / 'constant' / 'omit' / 'blocked'
    status: dict[tuple[str, str], str] = {}

    def resolve(key: tuple[str, str]) -> str:
        if key in status:
            return status[key]
        info = cells.get(key)
        if info is None:
            result = "omit"
        elif key[0] in workbook.hidden_sheets:
            result = "omit"
        elif info.spec.level not in policy.allowed_levels:
            result = "omit"
        elif key in tainted:
            result = "blocked"
        elif key in keep:
            deps_ok = not info.unknown_names and all(
                resolve(dep) in ("formula", "constant") for dep in info.dep_keys
            )
            if info.ast is not None and deps_ok:
                result = "formula"
            elif key in publish:
                result = "constant"
            else:
                result = "omit"
        elif key in publish:
            result = "constant"
        else:
            result = "omit"
        status[key] = result
        return result

    def omit_reason(key: tuple[str, str]) -> str:
        info = cells.get(key)
        if info is None:
            return "missing-cell"
        if key[0] in workbook.hidden_sheets:
            return "hidden-sheet"
        if info.spec.level not in policy.allowed_levels:
            return f"level-not-permitted:{info.spec.level}"
        if key in keep and info.ast is None and key not in publish:
            return "no-formula-to-keep"
        return "references-not-permitted"

    # 6) 求值器（在工作簿全量数据上求值；只有获准结果才会进入导出物）
    eval_memo: dict[tuple[str, str], Any] = {}

    def evaluate(key: tuple[str, str]) -> Any:
        if key in eval_memo:
            return eval_memo[key]
        info = cells.get(key)
        if info is None:
            raise EvaluationError(f"引用了不存在的单元格 {_canon_ref(key)}")
        if info.ast is None:
            value = info.spec.value
            if value is None:
                raise EvaluationError(f"空单元格 {_canon_ref(key)}")
            if not isinstance(value, (int, float, str, bool)):
                raise EvaluationError(f"不支持的值类型: {type(value).__name__}")
        else:
            value = _eval_ast(info.ast, _lookup)
        eval_memo[key] = value
        return value

    def _lookup(token: tuple) -> Any:
        if token[0] == "ref":
            return evaluate((token[1], token[2]))
        name = token[1]
        if name not in name_targets:
            raise EvaluationError(f"未定义的名称: {name!r}")
        targets, _canon = name_targets[name]
        if len(targets) != 1:
            raise EvaluationError(f"名称 {name!r} 指向区域，无法作为单值求值")
        return evaluate(next(iter(targets)))

    # 7) 生成导出格子
    result = ExportResult()
    result.cycles = [[_canon_ref(k) for k in cycle] for cycle in cycles]
    candidates = publish | keep

    for key in sorted(candidates):
        ref = _canon_ref(key)
        verdict = resolve(key)
        if verdict == "blocked":
            continue  # 见第 8 步统一报告
        if verdict == "omit":
            result.omitted[ref] = omit_reason(key)
            continue
        info = cells[key]
        try:
            value = evaluate(key)
        except EvaluationError as exc:
            result.errors[ref] = str(exc)
            continue
        formula = info.spec.formula if verdict == "formula" else None
        result.cells[ref] = ExportedCell(ref=ref, value=value, formula=formula)

    # 8) 循环报告：列出依赖环的导出结果及其依赖的环
    for key in sorted(candidates):
        if key not in tainted:
            continue
        hit: set[int] = set()
        seen: set[tuple[str, str]] = set()
        queue = [key]
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in cycle_member:
                hit.add(cycle_member[node])
            queue.extend(edges.get(node, ()))
        result.cycle_blocked[_canon_ref(key)] = [
            [_canon_ref(k) for k in cycles[idx]] for idx in sorted(hit)
        ]

    # 9) 重建名称表：仅保留被“保留公式”实际使用且目标获准导出的名称
    used_names: set[str] = set()
    for key, verdict in status.items():
        if verdict == "formula":
            used_names |= cells[key].names
    for name in sorted(used_names):
        targets, canonical = name_targets[name]
        if all(resolve(t) in ("formula", "constant") for t in targets):
            result.named_ranges[name] = canonical

    return result
