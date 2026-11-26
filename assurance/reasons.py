"""Stable rejection reason codes and Chinese operator-facing messages."""

from __future__ import annotations

SUCCESS = "SUCCESS"

# Input/configuration errors are raised; these codes are used for reports.
INVALID_FACT = "INVALID_FACT"
INVALID_RULE = "INVALID_RULE"

# Graph/route failures.
UNKNOWN_AUTHORITY = "UNKNOWN_AUTHORITY"
NO_OUTGOING_RULE = "NO_OUTGOING_RULE"
NO_ROUTE = "NO_ROUTE"
TARGET_AUTHORITY_NOT_REACHED = "TARGET_AUTHORITY_NOT_REACHED"
AUTHORITY_NOT_TRUSTED = "AUTHORITY_NOT_TRUSTED"
MAX_DEPTH_EXCEEDED = "MAX_DEPTH_EXCEEDED"

# Rule-condition failures.
SOURCE_LEVEL_MISMATCH = "SOURCE_LEVEL_MISMATCH"
SOURCE_LEVEL_TOO_LOW = "SOURCE_LEVEL_TOO_LOW"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
BUSINESS_NOT_ALLOWED = "BUSINESS_NOT_ALLOWED"
BUSINESS_SCOPE_EMPTY = "BUSINESS_SCOPE_EMPTY"
RULE_DISABLED = "RULE_DISABLED"
RULE_NOT_YET_EFFECTIVE = "RULE_NOT_YET_EFFECTIVE"
RULE_EXPIRED = "RULE_EXPIRED"
AUTH_TIME_TOO_OLD = "AUTH_TIME_TOO_OLD"
AUTH_TIME_TOO_RECENT = "AUTH_TIME_TOO_RECENT"
AUTH_TIME_IN_FUTURE = "AUTH_TIME_IN_FUTURE"
AUTH_TIME_BEFORE_WINDOW = "AUTH_TIME_BEFORE_WINDOW"
AUTH_TIME_AFTER_WINDOW = "AUTH_TIME_AFTER_WINDOW"

# A valid proof exists, but its mapped assurance level is insufficient.
LEVEL_TOO_LOW = "LEVEL_TOO_LOW"

# Fallback.
NO_PROOF_CHAIN = "NO_PROOF_CHAIN"

REASON_MESSAGES_ZH = {
    SUCCESS: "存在满足最低保证等级的有效证明链",
    INVALID_FACT: "认证事实结构无效",
    INVALID_RULE: "互认规则结构无效",
    UNKNOWN_AUTHORITY: "认证机构没有可用于互认的公布规则",
    NO_OUTGOING_RULE: "当前机构没有继续互认的规则",
    NO_ROUTE: "互认图中不存在满足条件的证明路径",
    TARGET_AUTHORITY_NOT_REACHED: "证明链未到达指定目标机构",
    AUTHORITY_NOT_TRUSTED: "证明链终点不在受信机构范围内",
    MAX_DEPTH_EXCEEDED: "搜索超过最大证明链长度",
    SOURCE_LEVEL_MISMATCH: "事实等级与规则要求的来源等级不匹配",
    SOURCE_LEVEL_TOO_LOW: "事实等级低于规则要求的来源等级",
    METHOD_NOT_ALLOWED: "该验证方式不在互认规则允许范围内",
    BUSINESS_NOT_ALLOWED: "该目标业务不在互认规则允许范围内",
    BUSINESS_SCOPE_EMPTY: "互认后目标业务范围为空",
    RULE_DISABLED: "互认规则未启用",
    RULE_NOT_YET_EFFECTIVE: "互认规则尚未生效",
    RULE_EXPIRED: "互认规则已过有效期",
    AUTH_TIME_TOO_OLD: "验证发生时间超过规则允许的新鲜度窗口",
    AUTH_TIME_TOO_RECENT: "验证发生时间早于规则要求的最早间隔",
    AUTH_TIME_IN_FUTURE: "验证发生时间位于未来，不能作为有效事实",
    AUTH_TIME_BEFORE_WINDOW: "验证发生时间早于规则允许的时间窗口",
    AUTH_TIME_AFTER_WINDOW: "验证发生时间晚于规则允许的时间窗口",
    LEVEL_TOO_LOW: "证明链有效，但最终保证等级低于业务最低要求",
    NO_PROOF_CHAIN: "找不到满足要求的有效证明链",
}


def message_for(code: str) -> str:
    return REASON_MESSAGES_ZH.get(code, code)
