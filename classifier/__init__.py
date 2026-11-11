"""响应语义判定引擎。

四层信号（不依赖单一状态码 / 正文长度 / 关键词）：
  1. 结构契约  structure  —— 地标区域、标签闭合、截断标记、骨架占比
  2. 明确标记  marker     —— 空结果 / 维护 / 访问提示 / 错误 的显式多语言标记
  3. 内容特征  content    —— 资料卡片、列表行数、正文实质内容
  4. 导航来源  navigation —— Referer / meta 声明的来源场景（兜底与解歧）

判定输出附带逐条证据、权重与置信边界；证据不足或类别接近时隔离为 UNKNOWN。
"""

from .engine import classify_response, FetchInput
from .custom_rules import RuleError, apply_custom_rules
from .models import (
    CATEGORIES,
    Category,
    Evidence,
    ClassificationResult,
    FetchResult,
)

__all__ = [
    "classify_response",
    "FetchInput",
    "apply_custom_rules",
    "RuleError",
    "CATEGORIES",
    "Category",
    "Evidence",
    "ClassificationResult",
    "FetchResult",
]
