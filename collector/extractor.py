# -*- coding: utf-8 -*-
"""测量记录提取器。

在数值字面量解析之上处理科研测量文本的完整语义：
- 缺失值：``-``、``—``、``N/A`` 等，保持空值，绝不转成 0；
- 检测限：``< L`` / ``> L``，数值仅作为界值，限定符标明低于/高于检测限，
  不当作精确等值；
- 区间：``a – b``、``a ~ b``、``a to b``、``a - b``（带空格连字符）；
- 不确定度：对称 ``x ± u``、括号 ``x(u)``、非对称 ``x +u1 -u2``；
- 单位白名单校验，非法单位保留数值并报错；
- 每条记录保留原文、数值、单位、限定符、精度与逐步解析解释。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from decimal import Decimal

from .locales import LocaleRule, NBSP, NNBSP, THINSP
from .parser import parse_number, normalize_minus, precision_info, ParseError
from .units import validate_unit, split_unit

# 缺失值标记（比较前会做 trim 与大小写归一）
MISSING_TOKENS = {"-", "—", "–", "−", "n/a", "na", "null", "none", "无数据", "缺失", ""}

# 区间分隔符：破折号/波浪号/英文 to/带空格连字符（不带空格的 '-' 视为负号）
RANGE_RE = re.compile(r"\s*(?:–|—|~|to|(?<=\s)-(?=\s))\s*", re.IGNORECASE)

PAREN_UNC_RE = re.compile(r"^(?P<head>.*\d)\((?P<par>\d+)\)(?P<tail>.*)$")


@dataclass
class Uncertainty:
    kind: str                       # symmetric | parenthesis | asymmetric
    plus: str | None = None         # Decimal 文本
    minus: str | None = None
    raw: str = ""
    decimal_places: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Measurement:
    raw_text: str
    status: str                     # ok | missing | below_LOD | above_LOD | range | error
    value: Decimal | None = None
    value_end: Decimal | None = None
    unit: str | None = None
    unit_raw: str | None = None
    qualifier: str | None = None
    uncertainty: Uncertainty | None = None
    precision: dict | None = None
    steps: list[str] = field(default_factory=list)
    error: dict | None = None
    # 来源位置由采集层填充
    source: dict | None = None

    def to_dict(self) -> dict:
        def f(v):
            return None if v is None else format(v, "f")
        return {
            "raw_text": self.raw_text,
            "status": self.status,
            "value": f(self.value),
            "value_end": f(self.value_end),
            "unit": self.unit,
            "unit_raw": self.unit_raw,
            "qualifier": self.qualifier,
            "uncertainty": None if self.uncertainty is None else self.uncertainty.to_dict(),
            "precision": self.precision,
            "steps": list(self.steps),
            "error": self.error,
            "source": self.source,
        }


def _err(raw: str, steps: list[str], exc: ParseError) -> Measurement:
    steps.extend(exc.steps)
    steps.append(f"解析失败：[{exc.code}] {exc.message}")
    return Measurement(
        raw_text=raw, status="error",
        qualifier=None, steps=steps,
        error={"code": exc.code, "message": exc.message},
    )


def _attach_unit(tail: str, m: Measurement, steps: list[str]) -> None:
    # 数值与单位之间允许普通空格或不换行/窄空白
    rest, unit_raw = split_unit(tail.replace(NBSP, " ").replace(NNBSP, " ")
                                .replace(THINSP, " "))
    if unit_raw is None and not rest.strip():
        return
    if rest.strip():
        # 数值与单位之间存在无法识别的文本
        m.status = "error"
        m.error = {"code": "UNPARSED_TAIL",
                   "message": f"数值后存在无法识别的文本 {rest.strip()!r}"}
        steps.append(m.error["message"])
        return
    res = validate_unit(unit_raw)
    m.unit_raw = res.raw
    if res.error:
        m.status = "error"
        m.error = {"code": "ILLEGAL_UNIT", "message": res.error}
        steps.append(res.error)
    else:
        m.unit = res.canonical
        steps.append(f"单位原文 {unit_raw!r} 通过白名单校验，规范为 {res.canonical!r}")


def _parenthesis_uncertainty(head: str, par: str, tail: str,
                             rule: LocaleRule, steps: list[str]):
    """处理 1.234(5) 形式：括号数字对齐尾数最后若干位。"""
    num = parse_number(head.strip(), rule)
    steps.extend(num.steps)
    d = num.canonical.split("e", 1)[0]
    dec_places = len(d.split(".", 1)[1]) if "." in d else 0
    k = len(par)
    scale = Decimal(10) ** (-(dec_places - k + 1))
    u = Decimal(int(par)) * scale
    steps.append(
        f"括号不确定度 ({par})：对齐尾数最后 {k} 位，换算为 ±{format(u, 'f')}"
    )
    unc = Uncertainty(kind="parenthesis", plus=format(u, "f"),
                      minus=format(u, "f"), raw=f"({par})",
                      decimal_places=max(dec_places, 0))
    return num, unc, tail


def parse_measurement(raw: str, rule: LocaleRule) -> Measurement:
    steps: list[str] = [f"开始解析测量文本 {raw!r}，适用规则：{rule.describe()}"]
    original = raw
    s, replaced = normalize_minus(raw.strip())
    if replaced:
        steps.append("将 Unicode 减号统一替换为 ASCII '-'（原文保留在 raw_text）")
    # 不换行/窄空白是 fr_FR/sv_SE 合法分组符，必须原样保留给数值解析器；
    # 仅普通 ASCII 空格用于切词。原文始终不动。
    work = s
    if any(ch in s for ch in (NBSP, NNBSP, THINSP)):
        steps.append("识别到不换行/窄空白字符：数字内部按地区分组规则处理，"
                     "普通空格仅用于切词，原文保持不变")

    # ---- 1. 缺失值 ----
    if work.strip().lower() in MISSING_TOKENS:
        steps.append(
            f"命中缺失值标记 {work.strip()!r}：记录为空值（None），不转换为 0"
        )
        return Measurement(raw_text=original, status="missing",
                           qualifier="missing", steps=steps,
                           precision=None)

    # ---- 1b. 普通空格把数字切成多段，但当前规则不接受空格分组 ----
    if re.search(r"\d\s+\d", work) and " " not in rule.grouping_seps:
        steps.append(
            f"检测到数字内部的空白分段，但当前规则（{rule.code}）不接受空格作为"
            "分组分隔符；拒绝把空白两侧强行拼接，请确认地区规则或改用该规则的"
            "规范分组符"
        )
        return Measurement(
            raw_text=original, status="error", qualifier=None, steps=steps,
            error={"code": "SPACE_GROUP_NOT_ALLOWED",
                   "message": f"地区规则 {rule.code} 不允许空格分组，"
                              "数字被空白切断，无法解析为单一数值"},
        )

    # ---- 2. 检测限 < / > ----
    lod = re.match(r"^([<>])\s*(?P<body>.+)$", work)
    if lod:
        sign, body = lod.group(1), lod.group("body").strip()
        steps.append(f"识别检测限标记 {sign!r}：后续数值仅为界值，不是精确等值")
        num_text, _, tail = body.partition(" ")
        try:
            num = parse_number(num_text, rule)
        except ParseError as exc:
            return _err(original, steps, exc)
        steps.extend(num.steps)
        m = Measurement(raw_text=original,
                        status="below_LOD" if sign == "<" else "above_LOD",
                        value=num.value,
                        qualifier=("below_detection_limit" if sign == "<"
                                   else "above_detection_limit"),
                        precision=precision_info(num.canonical),
                        steps=steps)
        steps.append(
            ("低于" if sign == "<" else "高于")
            + f"检测限：界值 {format(num.value, 'f')} 仅用于报告阈值，"
            "该记录不是精确等值，禁止当作精确测量值参与等值比较"
        )
        _attach_unit(tail if tail else body[len(num_text):], m, steps)
        return m

    # ---- 3. 区间 ----
    range_parts = RANGE_RE.split(work)
    if len(range_parts) == 2 and range_parts[0].strip() and range_parts[1].strip():
        steps.append("识别区间表达式，将分别解析上下界")
        try:
            lo_text, lo_tail = _split_num_and_tail(range_parts[0].strip())
            hi_text, hi_tail = _split_num_and_tail(range_parts[1].strip())
            lo = parse_number(lo_text, rule)
            hi = parse_number(hi_text, rule)
        except ParseError as exc:
            return _err(original, steps, exc)
        steps.extend(lo.steps)
        steps.extend(hi.steps)
        if lo.value > hi.value:
            return Measurement(
                raw_text=original, status="error", qualifier="range",
                steps=steps + [f"区间上下界倒置：{format(lo.value, 'f')} > {format(hi.value, 'f')}"],
                error={"code": "BAD_RANGE", "message": "区间下界大于上界"},
            )
        m = Measurement(raw_text=original, status="range",
                        value=lo.value, value_end=hi.value,
                        qualifier="range",
                        precision=precision_info(lo.canonical),
                        steps=steps)
        steps.append(
            f"区间解析成功：[{format(lo.value, 'f')}, {format(hi.value, 'f')}]，"
            "以单条区间记录保存，不合并为单点数值"
        )
        unit_tail = (lo_tail or "").strip() or (hi_tail or "").strip()
        _attach_unit(unit_tail, m, steps)
        return m

    # ---- 4. 括号不确定度 1.234(5) ----
    pm = PAREN_UNC_RE.match(work)
    if pm:
        try:
            num, unc, tail = _parenthesis_uncertainty(
                pm.group("head"), pm.group("par"), pm.group("tail"), rule, steps
            )
        except ParseError as exc:
            return _err(original, steps, exc)
        m = Measurement(raw_text=original, status="ok", value=num.value,
                        uncertainty=unc,
                        precision=precision_info(num.canonical),
                        steps=steps)
        steps.append("对称不确定度（括号记法）已保存，精度按尾数对齐")
        _attach_unit(tail, m, steps)
        return m

    # ---- 5. 对称不确定度 x ± u ----
    if "±" in work:
        left, right = work.split("±", 1)
        left, right = left.strip(), right.strip()
        num_text, num_tail = _split_num_and_tail(left)
        unc_text, unc_tail = _split_num_and_tail(right)
        try:
            num = parse_number(num_text, rule)
            unc_num = parse_number(unc_text, rule)
        except ParseError as exc:
            return _err(original, steps, exc)
        steps.extend(num.steps)
        steps.extend(unc_note(unc_num, steps, "±"))
        m = Measurement(raw_text=original, status="ok", value=num.value,
                        uncertainty=Uncertainty(
                            kind="symmetric",
                            plus=format(unc_num.value, "f"),
                            minus=format(unc_num.value, "f"),
                            raw="± " + unc_num.raw_text,
                            decimal_places=precision_info(unc_num.canonical)["decimal_places"],
                        ),
                        precision=precision_info(num.canonical),
                        steps=steps)
        tail = (num_tail or "").strip() or (unc_tail or "").strip()
        _attach_unit(tail, m, steps)
        return m

    # ---- 6. 非对称不确定度 x +u1 -u2 ----
    tokens = work.split()
    asym_idx = None
    for i in range(1, len(tokens) - 1):
        if tokens[i].startswith("+") and tokens[i + 1].startswith("-"):
            asym_idx = i
            break
    if asym_idx:
        head = " ".join(tokens[:asym_idx])
        up_raw = tokens[asym_idx][1:]
        down_raw = tokens[asym_idx + 1][1:]
        tail = " ".join(tokens[asym_idx + 2:])
        try:
            num = parse_number(head, rule)
            up = parse_number(up_raw, rule)
            down = parse_number(down_raw, rule)
        except ParseError as exc:
            return _err(original, steps, exc)
        steps.extend(num.steps)
        steps.append(
            f"识别非对称不确定度：+{format(up.value, 'f')} / -{format(down.value, 'f')}"
        )
        if up.value < 0 or down.value < 0:
            return Measurement(
                raw_text=original, status="error", steps=steps,
                error={"code": "BAD_UNCERTAINTY",
                       "message": "不确定度分量必须为非负数"},
            )
        m = Measurement(raw_text=original, status="ok", value=num.value,
                        uncertainty=Uncertainty(
                            kind="asymmetric",
                            plus=format(up.value, "f"),
                            minus=format(down.value, "f"),
                            raw=f"+{up_raw} -{down_raw}",
                        ),
                        precision=precision_info(num.canonical),
                        steps=steps)
        _attach_unit(tail, m, steps)
        return m

    # ---- 7. 普通单值 ----
    num_text, tail = _split_num_and_tail(work)
    try:
        num = parse_number(num_text, rule)
    except ParseError as exc:
        return _err(original, steps, exc)
    steps.extend(num.steps)
    m = Measurement(raw_text=original, status="ok", value=num.value,
                    precision=precision_info(num.canonical),
                    steps=steps)
    _attach_unit(tail, m, steps)
    if m.status == "error":
        steps.append(f"[{m.error['code']}] {m.error['message']}")
    return m


def _split_num_and_tail(text: str) -> tuple[str, str]:
    """从混合文本头部切出数值字面量（含地区分组空白与科学计数法），其余作为尾部。

    普通 ASCII 空格仅在“后接数字”时视为数字内部字符（如 fr 的 1 234,5）；
    “数字 空格 单位”中的空格后接字母，仍作为尾部边界。
    """
    text = text.strip()
    i = 0
    if i < len(text) and text[i] in "+-":
        i += 1
    while i < len(text):
        ch = text[i]
        if ch.isdigit() or ch in ".," or ch in (NBSP, NNBSP, THINSP):
            i += 1
            continue
        if ch == " " and i + 1 < len(text) and text[i + 1].isdigit():
            i += 1
            continue
        if ch in "eE" and i + 1 < len(text):
            j = i + 1
            if text[j] in "+-":
                j += 1
            if j < len(text) and text[j].isdigit():
                i = j
                while i < len(text) and text[i].isdigit():
                    i += 1
                continue
        break
    return text[:i].strip(), text[i:].strip()


def unc_note(unc_num, steps, marker):
    return [f"不确定度分量 {marker} {unc_num.raw_text!r} 解析为 "
            f"{format(unc_num.value, 'f')}（与主值同一地区规则）"]
