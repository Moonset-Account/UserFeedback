"""反序列化字段策略兼容核查组件。

读取配置层、协议版本范围与消息类型允许的字段策略，
输出每组生产者/消费者在重叠升级窗口内的兼容结论。
"""

from .audit import audit_config, audit_dict
from .config import ConfigError, load_config
from .model import (
    AuditReport,
    PairConclusion,
    PairStatus,
    ReasonCode,
    ResolvedOption,
)

__all__ = [
    "audit_config",
    "audit_dict",
    "load_config",
    "ConfigError",
    "AuditReport",
    "PairConclusion",
    "PairStatus",
    "ReasonCode",
    "ResolvedOption",
]

__version__ = "1.0.0"
