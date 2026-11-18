"""离线预检模块：评估对象前缀对匿名请求的开放范围。

读取组织边界、资源策略、继承的默认设置与前缀清单，
区分直接匿名访问与经批准分发入口的访问，输出每个前缀的
公开状态、公开范围及依据；对依赖快照外属性的策略条件标记待确认。
"""

from .evaluator import evaluate_snapshot
from .loader import load_snapshot

__all__ = ["evaluate_snapshot", "load_snapshot"]
