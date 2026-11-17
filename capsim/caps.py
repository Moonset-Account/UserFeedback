"""固定的十二项能力全集与位掩码工具。

全集选取与容器默认能力集同一口径（不含 CAP_SETFCAP，该能力不在本模型全集内），
索引顺序即位序，位掩码 int 在 CapState / 边门控中统一使用。
"""
from __future__ import annotations

from collections.abc import Iterable

# 任务限定的十二项能力全集（顺序即 bit 位序，勿调整）
CAP_NAMES: tuple[str, ...] = (
    "CAP_CHOWN",            # bit 0
    "CAP_DAC_OVERRIDE",     # bit 1
    "CAP_FOWNER",           # bit 2
    "CAP_FSETID",           # bit 3
    "CAP_KILL",             # bit 4
    "CAP_SETGID",           # bit 5
    "CAP_SETUID",           # bit 6
    "CAP_SETPCAP",          # bit 7
    "CAP_NET_BIND_SERVICE", # bit 8
    "CAP_NET_RAW",          # bit 9
    "CAP_SYS_CHROOT",       # bit 10
    "CAP_AUDIT_WRITE",      # bit 11
)

NAME_TO_BIT: dict[str, int] = {name: 1 << idx for idx, name in enumerate(CAP_NAMES)}
ALL_MASK: int = (1 << len(CAP_NAMES)) - 1


def to_mask(caps: Iterable[str] | int | None) -> int:
    """能力名序列转位掩码；int 原样校验后返回；None 视为空集。"""
    if caps is None:
        return 0
    if isinstance(caps, int):
        mask = caps
    else:
        mask = 0
        for name in caps:
            if name not in NAME_TO_BIT:
                raise ValueError(f"能力不在十二项全集内: {name!r}")
            mask |= NAME_TO_BIT[name]
    if mask & ~ALL_MASK:
        raise ValueError(f"位掩码超出十二项全集: {mask:#x}")
    return mask


def to_names(mask: int) -> list[str]:
    """位掩码转按全集顺序排列的能力名列表。"""
    if mask & ~ALL_MASK:
        raise ValueError(f"位掩码超出十二项全集: {mask:#x}")
    return [name for name, bit in NAME_TO_BIT.items() if mask & bit]
