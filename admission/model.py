"""准入模块的结构化输入模型。

输入被严格限定为这些数据类，以及由它们组成的控制流（
:class:`Sequence` / :class:`Branch` / :class:`UnknownBranch`）。
所有对象（文件、目录）都以调用方提供的不可变标识 ``id`` 区分；
路径与名字只是可复用的绑定，不携带任何授权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Union


class Verdict(str, Enum):
    """单个操作或整体程序的准入结论。"""

    ALLOW = "ALLOW"
    """满足全部所有权与私有处理条款。"""

    REVIEW = "REVIEW"
    """存在按分支未知的目录所有权或其他无法静态确定的状态，保留待复核。"""

    DENY = "DENY"
    """违反确定性的准入条款（未锁定目录接收对象、非私有源被移动等）。"""


@dataclass(frozen=True)
class EstablishDirectory:
    """在 ``path`` 建立一个新目录（临时目录创建）。

    建立本身不等于授权：目录只有随后按所有权条款锁定（:class:`LockDirectory`）
    后，才能接收文件或作为移动目标。
    """

    id: str
    """目录对象的不可变标识。"""
    path: str
    """目录路径（可复用的名字，不携带授权）。"""
    owner: str
    """创建身份：建立该目录的主体。"""


@dataclass(frozen=True)
class InheritDirectory:
    """继承一个已存在的目录。

    继承而来的目录所有权需要显式确认（随后锁定），确认前不可接收对象；
    本操作本身始终保留待复核。
    """

    id: str
    path: str
    expected_owner: Optional[str] = None
    """调用方声明的预期所有权，仅在后续锁定确认后才成立。"""


@dataclass(frozen=True)
class LockDirectory:
    """按所有权条款锁定目录。

    只有处于“已建立”状态、且锁定身份与创建身份一致的私有目录才能锁定成功。
    锁定后目录才获得接收对象的授权。
    """

    directory_id: str
    owner: str
    """申请锁定的身份，必须与目录建立者（或确认的所有者）一致。"""


@dataclass(frozen=True)
class CreateFile:
    """在指定目录内创建临时文件。"""

    id: str
    """文件对象的不可变标识。"""
    name: str
    """文件名（绑定，可复用）。"""
    directory_id: str
    """接收目录。"""
    creator: str
    """创建身份。"""
    private: bool = True
    """是否为私有处理对象；新建文件默认私有。"""


@dataclass(frozen=True)
class MoveObject:
    """把对象换名或移动到另一目录（两者可同时发生）。

    准入需要分别确认：
    1. 源对象处于私有处理状态；
    2. 目标目录可接收（已按所有权条款锁定，或继承目录已确认锁定）。
    """

    object_id: str
    target_directory_id: str
    target_name: Optional[str] = None
    """目标名字；为 ``None`` 时保留原名字（仅跨目录移动）。"""
    mover: str = ""
    """发起移动的身份，用于审计定位。"""


@dataclass(frozen=True)
class UnknownBranch:
    """控制流中按分支未知的目录所有权声明（合成输入）。

    表示存在一个静态无法展开的分支，其内部可能影响 ``affected_directory_ids``
    中的目录所有权，以及 ``affected_object_ids`` 中对象的可达性。
    被列为受影响的目录在分支汇合后所有权按未知处理，访问即保留待复核。
    """

    affected_directory_ids: List[str] = field(default_factory=list)
    affected_object_ids: List[str] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class Sequence:
    """顺序控制流。"""

    steps: List["Operation"] = field(default_factory=list)


@dataclass(frozen=True)
class Branch:
    """分支控制流：``alternatives`` 中的分支互斥执行后汇合。"""

    alternatives: List["Operation"] = field(default_factory=list)


# 结构化操作的联合类型。
Operation = Union[
    EstablishDirectory,
    InheritDirectory,
    LockDirectory,
    CreateFile,
    MoveObject,
    UnknownBranch,
    Sequence,
    Branch,
]
