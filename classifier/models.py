"""判定器数据模型与类别常量。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Category(str, Enum):
    """语义分类。

    VALID          有效资料页
    EMPTY          合法空结果（请求合法，但没有匹配资料）
    MAINTENANCE    维护页面
    ACCESS_NOTICE  访问提示（登录 / 权限 / 风控提示）
    ERROR_PAGE     错误页（站点自身报错，但 HTTP 仍为 200）
    BROKEN_TEMPLATE 模板残缺页（截断 / 骨架占位 / 标签未闭合）
    FETCH_FAILED   采集失败（传输层失败，不进入语义判定）
    UNKNOWN        不确定 —— 隔离区，等待人工补充规则后复判
    """

    VALID = "VALID"
    EMPTY = "EMPTY"
    MAINTENANCE = "MAINTENANCE"
    ACCESS_NOTICE = "ACCESS_NOTICE"
    ERROR_PAGE = "ERROR_PAGE"
    BROKEN_TEMPLATE = "BROKEN_TEMPLATE"
    FETCH_FAILED = "FETCH_FAILED"
    UNKNOWN = "UNKNOWN"


# 参与语义打分的类别（不含传输失败与隔离兜底）
SEMANTIC_CATEGORIES = [
    Category.VALID,
    Category.EMPTY,
    Category.MAINTENANCE,
    Category.ACCESS_NOTICE,
    Category.ERROR_PAGE,
    Category.BROKEN_TEMPLATE,
]

CATEGORIES = [c.value for c in Category]

# 分类中文展示名
CATEGORY_LABELS_ZH = {
    Category.VALID.value: "有效资料页",
    Category.EMPTY.value: "合法空结果",
    Category.MAINTENANCE.value: "维护页面",
    Category.ACCESS_NOTICE.value: "访问提示",
    Category.ERROR_PAGE.value: "错误页（HTTP 200）",
    Category.BROKEN_TEMPLATE.value: "模板残缺页",
    Category.FETCH_FAILED.value: "采集失败",
    Category.UNKNOWN.value: "不确定（已隔离）",
}

# 判定隔离与置信边界参数
MIN_CONFIDENCE = 0.55          # 低于该值一律隔离
MIN_MARGIN = 0.12              # 第一名与第二名分差低于该值视为歧义，隔离
BROKEN_HARD_BONUS = 0.45       # 截断硬信号给残缺类别的硬性加成
BROKEN_CONFIDENCE_CAP = 0.90   # 截断类置信上限（仍保留人工复核空间）


@dataclass
class Evidence:
    """单条判定证据。

    weight 为正表示支持该类别，为负表示反对；layer 标明证据所属信号层。
    """

    code: str
    layer: str                 # structure / marker / content / navigation / rule / meta
    message: str               # 中文人类可读说明
    weight: float
    supports: str              # Category.value
    excerpt: str = ""          # 页面中的原始片段（截断展示）
    region: str = ""           # 证据所在页面区域

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClassificationResult:
    category: str
    confidence: float
    margin: float
    scores: dict[str, float]
    evidences: list[Evidence]
    isolated: bool
    isolation_reasons: list[str] = field(default_factory=list)
    broken_gate: bool = False
    custom_rule_codes: list[str] = field(default_factory=list)
    engine_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "category_label": CATEGORY_LABELS_ZH.get(self.category, self.category),
            "confidence": round(self.confidence, 4),
            "confidence_lower_bound": round(max(0.0, self.confidence - self.margin), 4),
            "confidence_upper_bound": round(min(1.0, self.confidence + self.margin), 4),
            "margin": round(self.margin, 4),
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "evidences": [e.to_dict() for e in self.evidences],
            "isolated": self.isolated,
            "isolation_reasons": self.isolation_reasons,
            "broken_gate": self.broken_gate,
            "custom_rule_codes": self.custom_rule_codes,
            "engine_version": self.engine_version,
        }


@dataclass
class FetchResult:
    """采集器输出 —— 判定器唯一的输入形态（不接触任何真值）。"""

    ok: bool
    url: str
    status_code: int | None = None
    content_type: str = ""
    html: str = ""
    fetched_at: str = ""
    elapsed_ms: int = 0
    fetch_mode: str = "http"          # http / browser / file
    referer: str = ""
    nav_source: str = ""              # 采集器从 meta / query 解析出的导航来源
    error: str = ""
    rendered_html: str = ""           # 浏览器模式下的渲染后 DOM（判定仍用原始 html）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchInput:
    """判定引擎的轻量输入（便于复判时从存储重建）。"""

    html: str
    status_code: int = 200
    url: str = ""
    content_type: str = "text/html; charset=utf-8"
    referer: str = ""
    nav_source: str = ""
