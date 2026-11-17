"""有限域 GF(65537) 上的阈值秘密重建库（含错误份额纠正）。

每个字节由一个次数小于 t 的独立多项式共享，多项式常数项即该字节值。
重建阶段调用方声明最多 e 个错误份额位置；剔除缺失份额后，至少需要
t + 2e 个不同横坐标才进入纠错重建（Berlekamp-Welch）。

重建后强制校验：
  * 所有列共同涉及的错误份额位置并集不超过 e；
  * 每个多项式常数项可表示为一个字节（0..255）；
  * 恢复原文与批次描述中的 SHA-256 摘要一致。

任一校验失败都返回分类结果而不返回任何秘密字节；诊断信息只列份额编号。
"""

from .recovery import (
    FIELD_PRIME,
    MAX_BYTES,
    MAX_SHARES,
    BatchDescriptor,
    InconsistencyReason,
    RecoveryResult,
    RecoveryStatus,
    Share,
    recover,
)

__all__ = [
    "FIELD_PRIME",
    "MAX_BYTES",
    "MAX_SHARES",
    "BatchDescriptor",
    "InconsistencyReason",
    "RecoveryResult",
    "RecoveryStatus",
    "Share",
    "recover",
]
