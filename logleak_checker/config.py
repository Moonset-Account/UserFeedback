"""检查组件的配置：敏感参数、日志函数、掩码函数、已知安全函数。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet

#: 常见无副作用内建函数，默认视为“分析范围内、不改变污点”。
_DEFAULT_KNOWN_SAFE = frozenset(
    {
        "str",
        "repr",
        "len",
        "int",
        "float",
        "bool",
        "bytes",
        "format",
        "hex",
        "id",
        "isinstance",
        "getattr",
        "enumerate",
        "zip",
        "sorted",
        "list",
        "tuple",
        "set",
        "dict",
    }
)

_DEFAULT_SENSITIVE_PARAMS = frozenset(
    {
        "email",
        "user_email",
        "phone",
        "phone_number",
        "mobile",
        "mobile_number",
    }
)

_DEFAULT_LOG_FUNCTIONS = frozenset(
    {
        "logging.debug",
        "logging.info",
        "logging.warning",
        "logging.error",
        "logging.critical",
        "logging.exception",
        "logging.log",
        "print",
        "logger.debug",
        "logger.info",
        "logger.warning",
        "logger.error",
        "logger.critical",
        "logger.exception",
        "logger.log",
    }
)

_DEFAULT_MASK_FUNCTIONS = frozenset(
    {
        "mask_email",
        "mask_phone",
        "mask_sensitive",
        "redact",
        "redact_email",
        "redact_phone",
    }
)


@dataclass(frozen=True)
class AnalyzerConfig:
    """分析配置。

    - sensitive_params: 视为敏感数据的函数参数名（完整邮箱、手机号等）。
    - log_functions: 日志汇聚点，可写点分路径（``logging.info``）或裸名（``info``）。
    - mask_functions: 评审认可的脱敏函数，返回值视为已掩码。
    - known_safe_functions: 已知安全、仅透传污点的函数；不在任何集合中的
      调用会被标为“待人工确认”。
    """

    sensitive_params: FrozenSet[str] = field(default_factory=lambda: _DEFAULT_SENSITIVE_PARAMS)
    log_functions: FrozenSet[str] = field(default_factory=lambda: _DEFAULT_LOG_FUNCTIONS)
    mask_functions: FrozenSet[str] = field(default_factory=lambda: _DEFAULT_MASK_FUNCTIONS)
    known_safe_functions: FrozenSet[str] = field(default_factory=lambda: _DEFAULT_KNOWN_SAFE)

    @classmethod
    def from_json(cls, path: str | Path) -> "AnalyzerConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        base = cls()
        return cls(
            sensitive_params=frozenset(data.get("sensitive_params", base.sensitive_params)),
            log_functions=frozenset(data.get("log_functions", base.log_functions)),
            mask_functions=frozenset(data.get("mask_functions", base.mask_functions)),
            known_safe_functions=frozenset(
                data.get("known_safe_functions", base.known_safe_functions)
            ),
        )


def default_config() -> AnalyzerConfig:
    return AnalyzerConfig()
