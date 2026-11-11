# -*- coding: utf-8 -*-
"""地区数字格式规则。

规则只来自明确来源：
1. 源数据/页面内显式声明（JSON 字段、CSV 表头、HTML meta/data 属性）；
2. 用户在页面上的显式选择。

解析器从不根据字面量自行猜测地区。``UND``（未定）规则下遇到歧义字面量
（例如单独出现的 ``1,234``）会直接返回歧义错误，要求先选定地区。
"""
from __future__ import annotations

from dataclasses import dataclass

# 常见不换行 / 窄空白字符
NBSP = " "          # NO-BREAK SPACE
NNBSP = " "        # NARROW NO-BREAK SPACE
THINSP = " "       # THIN SPACE
FIGSP = " "        # FIGURE SPACE

SPACE_LIKE = (NBSP, NNBSP, THINSP, FIGSP)


@dataclass(frozen=True)
class LocaleRule:
    code: str
    label_zh: str
    decimal_sep: str          # 小数点：'.' 或 ','
    grouping_seps: tuple[str, ...]  # 合法分组分隔符（可为空元组）
    has_grouping: bool


    def describe(self) -> str:
        if not self.has_grouping:
            return f"{self.label_zh}（{self.code}）：小数点 '{self.decimal_sep}'，不启用分组分隔符"
        seps = "、".join(f"'{s}'" for s in self.grouping_seps)
        return f"{self.label_zh}（{self.code}）：小数点 '{self.decimal_sep}'，分组分隔符 {seps}"


# 支持的地区规则子集（CLDR 通用数字格式的常用子集）
RULES: dict[str, LocaleRule] = {
    "en_US": LocaleRule(
        code="en_US", label_zh="英语（美国）",
        decimal_sep=".", grouping_seps=(",",), has_grouping=True,
    ),
    "zh_CN": LocaleRule(
        code="zh_CN", label_zh="中文（中国）",
        decimal_sep=".", grouping_seps=(",",), has_grouping=True,
    ),
    "de_DE": LocaleRule(
        code="de_DE", label_zh="德语（德国）",
        decimal_sep=",", grouping_seps=(".",), has_grouping=True,
    ),
    "fr_FR": LocaleRule(
        code="fr_FR", label_zh="法语（法国）",
        # 规范分组符为不换行/窄不换行空格；协议子集额外接受普通空格
        # （真实数据中常被替代），解析时需数字前瞻，不会与“数字 单位”分隔冲突
        decimal_sep=",", grouping_seps=(NBSP, NNBSP, " "), has_grouping=True,
    ),
    "sv_SE": LocaleRule(
        code="sv_SE", label_zh="瑞典语（瑞典）",
        decimal_sep=",", grouping_seps=(NNBSP, NBSP, " "), has_grouping=True,
    ),
    # 未定地区：仅接受无分组的点小数（如 12.5、7.8E-3）；
    # 出现逗号/分组等歧义一律报错，要求用户显式选择地区。
    "und": LocaleRule(
        code="und", label_zh="地区未定（不猜测）",
        decimal_sep=".", grouping_seps=(), has_grouping=False,
    ),
}

DEFAULT_LOCALE = "und"


def get_rule(code: str | None) -> LocaleRule:
    if not code:
        return RULES[DEFAULT_LOCALE]
    key = code.strip()
    if key not in RULES:
        raise KeyError(f"不支持的地区规则代码: {code!r}，可选: {', '.join(RULES)}")
    return RULES[key]


def locale_choices() -> list[dict[str, str]]:
    return [{"code": c, "label_zh": r.label_zh} for c, r in RULES.items()]
