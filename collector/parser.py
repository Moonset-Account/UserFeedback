# -*- coding: utf-8 -*-
"""数值字面量解析核心。

设计原则：
- 保留原文（raw_text），解析全程产出人类可读的逐步解释（steps）；
- 地区规则必须显式传入，不猜测；未定地区下的歧义字面量返回 AMBIGUOUS 错误；
- 内部使用 Decimal，避免浮点误差；
- 支持：十进制点/逗号、千分位分组、不换行空格、科学计数法（ASCII e/E）、
  Unicode 减号；不支持罗马数字、中文数字等其他写法。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .locales import LocaleRule, SPACE_LIKE

# 错误码
ERR_AMBIGUOUS = "AMBIGUOUS_LOCALE"
ERR_BAD_GROUP = "BAD_GROUPING"
ERR_BAD_CHAR = "UNSUPPORTED_CHARACTER"
ERR_BAD_NUMBER = "MALFORMED_NUMBER"
ERR_BAD_EXP = "MALFORMED_EXPONENT"


class ParseError(Exception):
    def __init__(self, code: str, message: str, steps: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.steps = steps or []


@dataclass
class NumberResult:
    raw_text: str
    canonical: str | None            # 规范化 ASCII 数字文本（Decimal 可解析）
    value: Decimal | None            # Decimal 值
    locale_code: str
    steps: list[str] = field(default_factory=list)
    grouping_removed: bool = False
    scientific: bool = False

    def to_dict(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "canonical": self.canonical,
            "value": None if self.value is None else format(self.value, "f"),
            "locale_code": self.locale_code,
            "steps": list(self.steps),
            "grouping_removed": self.grouping_removed,
            "scientific": self.scientific,
        }


_MINUS_CHARS = ("−", "–", "—")  # Unicode 减号 / 短横 / 长横，统一为 ASCII '-'
_DIGITS = set("0123456789")
_EXP_MARKERS = ("e", "E")


def normalize_minus(text: str) -> tuple[str, bool]:
    out, replaced = text, False
    for ch in _MINUS_CHARS:
        if ch in out:
            out = out.replace(ch, "-")
            replaced = True
    return out, replaced


def _decimal_places(canonical_mantissa: str) -> int:
    if "." in canonical_mantissa:
        return len(canonical_mantissa.split(".", 1)[1])
    return 0


def _significant_digits(canonical: str) -> int:
    """规范化数字（可能含 e）的有效数字位数。

    规则：去掉前导 0 与小数点后，剩余数字全部计数，例如
    12000 → 5 位（不臆断末尾 0 为占位符），0.0078 → 2 位，1234.56 → 6 位。
    """
    body = canonical.split("e", 1)[0].split("E", 1)[0]
    neg = body.lstrip("-+")
    digits = neg.replace(".", "").lstrip("0")
    if not digits:
        # 数值为 0：以小数位数体现精度；整数 0 记 1 位
        frac = neg.split(".", 1)[1] if "." in neg else ""
        return len(frac.rstrip("0")) if frac else 1
    return len(digits)


def parse_number(raw: str, rule: LocaleRule) -> NumberResult:
    """按给定地区规则解析单个数值字面量。失败抛 ParseError（含步骤）。"""
    steps: list[str] = []
    original = raw
    text = raw.strip()
    steps.append(f"收到原文 {original!r}，去除首尾空白后为 {text!r}")

    text, minus_replaced = normalize_minus(text)
    if minus_replaced:
        steps.append("将 Unicode 减号（−/–/—）统一替换为 ASCII '-'")

    if not text:
        raise ParseError(ERR_BAD_NUMBER, "空字符串不是数值", steps)

    # 符号位
    sign = ""
    if text[0] in "+-":
        sign = text[0]
        text = text[1:]
        steps.append(f"识别符号位 {sign!r}")
    if not text:
        raise ParseError(ERR_BAD_NUMBER, "仅有符号、缺少数字", steps)

    # 科学计数法拆分（仅支持 ASCII e/E）
    scientific = False
    exp_text = ""
    exp_pos = -1
    for marker in _EXP_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            if exp_pos != -1:
                raise ParseError(ERR_BAD_EXP, "出现多个指数标记", steps)
            exp_pos = idx
    if exp_pos != -1:
        scientific = True
        mantissa_text = text[:exp_pos]
        exp_text = text[exp_pos + 1:]
        steps.append(f"识别科学计数法：尾数 {mantissa_text!r}，指数部分 {exp_text!r}")
    else:
        mantissa_text = text

    if not mantissa_text:
        raise ParseError(ERR_BAD_NUMBER, "缺少尾数数字", steps)

    # ---- 尾数按地区规则校验 ----
    dec = rule.decimal_sep
    group_seps = rule.grouping_seps
    all_seps = set(group_seps) | {dec}

    for ch in mantissa_text:
        if ch not in _DIGITS and ch not in all_seps:
            # und 规则下逗号/空格等不属于任何已定义分隔符，本质是地区歧义而非字符非法
            if rule.code == "und" and ch in (",", " "):
                steps.append(
                    "当前为“地区未定”规则：逗号/空白既可能是小数点也可能是分组符，"
                    "解析器拒绝猜测，请显式选择地区后重新解析"
                )
                raise ParseError(
                    ERR_AMBIGUOUS,
                    f"原文 {original!r} 在“地区未定”规则下存在歧义：无法确定 {ch!r} "
                    "是小数点还是分组分隔符，请在页面设置中显式选择地区后重新解析",
                    steps,
                )
            raise ParseError(
                ERR_BAD_CHAR,
                f"尾数含不受支持字符 {ch!r}；本协议子集仅支持阿拉伯数字、"
                f"小数点 {dec!r} 与规则内分组分隔符",
                steps,
            )

    # 先分离整数部分与小数部分（小数点最多一个）
    if dec in mantissa_text:
        int_part, frac_part = mantissa_text.split(dec, 1)
        if frac_part == "":
            raise ParseError(ERR_BAD_NUMBER, "小数点后缺少数字", steps)
    else:
        int_part, frac_part = mantissa_text, ""

    grouping_removed = False
    for sep in group_seps:
        if sep in int_part:
            grouping_removed = True
            if sep == dec:  # 理论上不会发生，防御
                raise ParseError(ERR_BAD_NUMBER, "分组分隔符与小数点冲突", steps)
            parts = int_part.split(sep)
            if any(part == "" for part in parts):
                raise ParseError(ERR_BAD_GROUP, f"分组分隔符 {sep!r} 相邻或位于首尾", steps)
            # 首组 1-3 位，其余组必须 3 位（小数部分不参与分组校验）
            head, tails = parts[0], parts[1:]
            if not (1 <= len(head) <= 3) or any(len(p) != 3 for p in tails):
                raise ParseError(
                    ERR_BAD_GROUP,
                    f"分组位数非法：以 {sep!r} 分组时首组应为 1-3 位、其余各组 3 位",
                    steps,
                )
            steps.append(
                f"按地区规则识别分组分隔符 {sep!r}，整数部分分组为 {parts}，移除分组符"
            )
            int_part = int_part.replace(sep, "")

    # 小数部分只允许数字（分组符不得出现在小数点后）
    if frac_part and any(ch not in _DIGITS for ch in frac_part):
        raise ParseError(ERR_BAD_GROUP, "小数部分内出现分组分隔符，分组仅允许位于整数部分", steps)

    # 未定地区的歧义判定：逗号/空白分组在 und 规则下均无定义
    if rule.code == "und":
        if "," in int_part or any(s in original for s in SPACE_LIKE):
            raise ParseError(
                ERR_AMBIGUOUS,
                f"原文 {original!r} 在“地区未定”规则下存在歧义：无法确定 ','/空白"
                "是小数点还是分组分隔符，请在页面设置中显式选择地区后重新解析",
                steps,
            )

    # 重组尾数并把小数点转 ASCII '.'
    if frac_part:
        mantissa_text = int_part + "." + frac_part
        if dec != ".":
            steps.append(f"将小数点 {dec!r} 转换为 ASCII '.'")
    else:
        mantissa_text = int_part

    if dec == "." and "," in mantissa_text:
        # 走到这里说明规则不允许逗号分组（如 und 已在上面拦截），防御性报错
        raise ParseError(
            ERR_AMBIGUOUS,
            f"当前地区规则不接受逗号，原文 {original!r} 存在歧义，拒绝猜测",
            steps,
        )

    if not re.fullmatch(r"\d+(\.\d+)?", mantissa_text):
        raise ParseError(ERR_BAD_NUMBER, f"规范化后的尾数 {mantissa_text!r} 仍不合法", steps)

    # ---- 指数校验 ----
    canonical_exp = ""
    if scientific:
        if not exp_text:
            raise ParseError(ERR_BAD_EXP, "指数标记后缺少数字", steps)
        e_sign = ""
        e_digits = exp_text
        if e_digits[0] in "+-":
            e_sign = e_digits[0]
            e_digits = e_digits[1:]
        if not e_digits or any(ch not in _DIGITS for ch in e_digits):
            raise ParseError(ERR_BAD_EXP, f"指数部分 {exp_text!r} 必须为整数", steps)
        canonical_exp = "e" + (e_sign or "") + e_digits
        steps.append(f"指数 {exp_text!r} 合法，规范化为 {canonical_exp!r}")

    canonical = (sign or "") + mantissa_text + canonical_exp
    try:
        value = Decimal(canonical)
    except InvalidOperation:
        raise ParseError(ERR_BAD_NUMBER, f"无法构造数值：{canonical!r}", steps)

    mantissa_for_precision = mantissa_text
    d_places = _decimal_places(mantissa_for_precision)
    sig = _significant_digits(canonical)
    steps.append(
        f"规范化数字为 {canonical!r}（Decimal={format(value, 'f')}），"
        f"小数位数 {d_places}，有效数字 {sig} 位"
    )

    return NumberResult(
        raw_text=original,
        canonical=canonical,
        value=value,
        locale_code=rule.code,
        steps=steps,
        grouping_removed=grouping_removed,
        scientific=scientific,
    )


def precision_info(canonical: str) -> dict:
    """供 extractor 复用的精度信息。"""
    mantissa = canonical.split("e", 1)[0].split("E", 1)[0]
    return {
        "decimal_places": _decimal_places(mantissa.lstrip("+-")),
        "significant_digits": _significant_digits(canonical),
    }
