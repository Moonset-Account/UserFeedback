"""场景注册表与独立真值（测试专用模块）。

真值仅允许 tests/ 与样例导出使用；
collector / classifier / runner / webapp 不得导入本模块
（tests/test_architecture.py 以 AST 强制）。
控制台需要的公开元数据请走 source_site.catalog。
"""

from __future__ import annotations

from .catalog import (
    PUBLIC_SCENARIOS,
    PUBLIC_TRANSPORT_SCENARIOS,
)
from .templates import BUILDERS

# 独立期望分类（与公开目录按 id 对齐；本映射就是“真值”）
_EXPECTED = {
    "normal": "VALID",
    "normal-keyword": "VALID",
    "empty-zh": "EMPTY",
    "empty-en": "EMPTY",
    "empty-ja": "EMPTY",
    "maintenance-zh": "MAINTENANCE",
    "maintenance-en": "MAINTENANCE",
    "maintenance-ja": "MAINTENANCE",
    "access-login-zh": "ACCESS_NOTICE",
    "access-permission-zh": "ACCESS_NOTICE",
    "access-captcha-zh": "ACCESS_NOTICE",
    "access-en": "ACCESS_NOTICE",
    "access-ja": "ACCESS_NOTICE",
    "error-zh": "ERROR_PAGE",
    "error-en": "ERROR_PAGE",
    "broken-truncated": "BROKEN_TEMPLATE",
    "broken-skeleton": "BROKEN_TEMPLATE",
    "broken-unclosed": "BROKEN_TEMPLATE",
    "ambiguous": "UNKNOWN",
    "uncertain-busy": "UNKNOWN",
    "maintenance-500": "MAINTENANCE",
    "tcp-reset": "FETCH_FAILED",
}

SCENARIOS = [
    {**s, "expect": _EXPECTED[s["id"]]} for s in PUBLIC_SCENARIOS
]
TRANSPORT_SCENARIOS = [
    {**s, "expect": _EXPECTED[s["id"]]} for s in PUBLIC_TRANSPORT_SCENARIOS
]

ALL_SCENARIOS = SCENARIOS + TRANSPORT_SCENARIOS
FULL_SUITE_IDS = [s["id"] for s in ALL_SCENARIOS]

BY_ID = {s["id"]: s for s in ALL_SCENARIOS}
PATH_TO_ID = {s["path"]: s["id"] for s in ALL_SCENARIOS}


def ground_truth_records() -> list[dict]:
    """独立真值记录（测试专用）。"""
    return [
        {
            "scenario_id": s["id"],
            "path": s["path"],
            "expected_category": s["expect"],
            "http_status": s["status"],
            "title": s["title"],
            "note": s["note"],
        }
        for s in ALL_SCENARIOS
    ]


def validate_against_builders() -> list[str]:
    """启动时自检：所有语义场景都必须有对应页面构造器。"""
    return [
        f"场景 {s['id']} 缺少页面构造器"
        for s in SCENARIOS
        if s["id"] not in BUILDERS
    ]
