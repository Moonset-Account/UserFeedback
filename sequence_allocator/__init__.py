"""关系数据库原型用的序列分配器。

公开 API：

- :class:`SequenceConfig`：序列配置（起点、步长、上下界、缓存批大小、是否绕回）。
- :class:`SequenceCatalog`：序列目录，负责创建/删除/打开会话/管理变更/持久化。
- :class:`SequenceSession`：会话，先预留小批号码再逐次 :meth:`nextval` 发放。
- 异常体系：:class:`SequenceError` 及其子类。
"""

from .catalog import SequenceCatalog
from .exceptions import (
    SequenceConfigError,
    SequenceError,
    SequenceExhaustedError,
    SequenceNotFoundError,
    SequenceSessionClosedError,
)
from .model import SequenceConfig, SequenceSession, SequenceState

__all__ = [
    "SequenceConfig",
    "SequenceCatalog",
    "SequenceSession",
    "SequenceState",
    "SequenceError",
    "SequenceConfigError",
    "SequenceNotFoundError",
    "SequenceExhaustedError",
    "SequenceSessionClosedError",
]

__version__ = "1.0.0"
