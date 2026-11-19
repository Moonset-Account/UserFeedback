"""专网隧道提案协商失败类型。"""

from __future__ import annotations


class NegotiationError(Exception):
    """所有协商失败的基类。"""


class UnknownAlgorithmError(NegotiationError):
    """提案引用了政策强度表中不存在的算法或分组。"""


class NoCommonProposalError(NegotiationError):
    """结构性匹配失败：双方不存在任何一对可组成完整套件的提案。

    该失败与网关强度政策无关——即使取消强度门槛，双方仍然没有
    共同的完整套件（可能原因：算法/分组不相交、AEAD 另配完整性、
    所需算法分散在不同提案中无法拼接等）。
    """

    def __init__(self, message: str, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


class PolicyRejectionError(NegotiationError):
    """政策淘汰：结构性共同套件存在，但全部未达到政策强度门槛。"""

    def __init__(self, message: str, *, rejected=None, min_strength: int | None = None):
        super().__init__(message)
        self.rejected = list(rejected or [])
        self.min_strength = min_strength
