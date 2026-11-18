"""锁序审查库：分析终端防护代理处置动作流中的潜在阻断（死锁）风险。

读取维护者导出的抽象动作流与支持的配置组合，仅当锁组合能在同一配置里
并发出现且相互等待时，才判为可能阻断处置。
"""

from .analyzer import review, review_file
from .model import Spec, Stream, load_spec

__all__ = ["review", "review_file", "Spec", "Stream", "load_spec"]
