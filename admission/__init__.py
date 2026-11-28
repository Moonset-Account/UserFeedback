"""本地生成代码的静态准入模块。

只接受结构化操作对象与控制流描述，不读取任何外部输入。
公开 API 见 :mod:`admission.model` 与 :mod:`admission.analyzer`。
"""

from .model import (
    Branch,
    CreateFile,
    EstablishDirectory,
    InheritDirectory,
    LockDirectory,
    MoveObject,
    Sequence,
    UnknownBranch,
    Verdict,
)
from .analyzer import AuditEntry, AuditReport, admit

__all__ = [
    "Branch",
    "CreateFile",
    "EstablishDirectory",
    "InheritDirectory",
    "LockDirectory",
    "MoveObject",
    "Sequence",
    "UnknownBranch",
    "Verdict",
    "AuditEntry",
    "AuditReport",
    "admit",
]
