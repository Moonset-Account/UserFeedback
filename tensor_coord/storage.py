"""物理权重存储与基于形状/步长的逻辑视图。"""

from __future__ import annotations

import math
from typing import Tuple

__all__ = ["PhysicalStorage", "View", "InvalidViewError"]


class InvalidViewError(ValueError):
    """视图定义或其到物理存储的映射非法。"""


def _plain_int(value, name: str) -> int:
    """要求真正的 int（拒绝 bool，因为 bool 是 int 的子类）。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidViewError(f"{name} 必须是整数，得到 {type(value).__name__}")
    return value


class PhysicalStorage:
    """一段连续、扁平的物理权重缓冲。"""

    def __init__(self, data):
        if not hasattr(data, "__iter__"):
            raise TypeError("storage data 必须是数值的可迭代对象")
        buffer = []
        for item in data:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise TypeError(
                    f"存储元素必须是 int/float，得到 {type(item).__name__}"
                )
            value = float(item)
            if math.isnan(value):
                raise ValueError("存储权重不允许 NaN")
            buffer.append(value)
        if not buffer:
            raise ValueError("物理存储不能为空")
        self._buffer = buffer

    # ---- 基本属性 -------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def buffer(self) -> list[float]:
        """返回缓冲副本，避免外部直接篡改物理权重。"""
        return list(self._buffer)

    def __len__(self) -> int:
        return len(self._buffer)

    def __getitem__(self, index: int) -> float:
        return self._buffer[index]

    def __iter__(self):
        return iter(self._buffer)

    def __eq__(self, other) -> bool:
        if not isinstance(other, PhysicalStorage):
            return NotImplemented
        return self._buffer == other._buffer

    def __repr__(self) -> str:
        return f"PhysicalStorage({self._buffer!r})"


class View:
    """由 ``offset + shape + strides`` 定义在物理存储上的逻辑视图。

    逻辑位置 ``idx`` 映射到物理位置
    ``offset + sum(idx[d] * strides[d])``。

    视图可以通过 :meth:`rebind` 重绑到另一个**等长**存储；已经被
    协调器固定（快照）的贡献不会因此改变。
    """

    def __init__(
        self,
        storage: PhysicalStorage,
        shape,
        strides,
        offset: int = 0,
    ):
        if not isinstance(storage, PhysicalStorage):
            raise TypeError("storage 必须是 PhysicalStorage 实例")
        shape = self._check_dim_tuple(shape, "shape", allow_zero=False)
        strides = self._check_dim_tuple(strides, "strides", allow_zero=True)
        if len(shape) != len(strides):
            raise InvalidViewError("shape 与 strides 维度数必须一致")
        offset = _plain_int(offset, "offset")
        if offset < 0:
            raise InvalidViewError("offset 不能为负")

        self._storage = storage
        self._shape: Tuple[int, ...] = shape
        self._strides: Tuple[int, ...] = strides
        self._offset = offset
        self._resolved_cache: Tuple[int, ...] | None = None

    @staticmethod
    def _check_dim_tuple(value, name: str, allow_zero: bool) -> Tuple[int, ...]:
        if not isinstance(value, (tuple, list)):
            raise InvalidViewError(f"{name} 必须是 tuple/list")
        out = []
        for dim in value:
            dim = _plain_int(dim, name)
            if dim < 0:
                raise InvalidViewError(f"{name} 维度不能为负")
            if not allow_zero and dim <= 0:
                raise InvalidViewError(f"{name} 维度必须为正整数")
            out.append(dim)
        if not out:
            raise InvalidViewError(f"{name} 至少需要一个维度")
        return tuple(out)

    # ---- 属性 -----------------------------------------------------
    @property
    def storage(self) -> PhysicalStorage:
        return self._storage

    @property
    def shape(self) -> Tuple[int, ...]:
        return self._shape

    @property
    def strides(self) -> Tuple[int, ...]:
        return self._strides

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def element_count(self) -> int:
        total = 1
        for dim in self._shape:
            total *= dim
        return total

    # ---- 重绑 -----------------------------------------------------
    def rebind(self, new_storage: PhysicalStorage) -> None:
        """把视图重绑到另一个等长存储，使旧映射失效后重新解析。"""
        if not isinstance(new_storage, PhysicalStorage):
            raise TypeError("new_storage 必须是 PhysicalStorage 实例")
        if new_storage.size != self._storage.size:
            raise InvalidViewError(
                "重绑目标存储长度必须与原存储一致："
                f"{new_storage.size} != {self._storage.size}"
            )
        self._storage = new_storage
        self._resolved_cache = None

    # ---- 映射解析 -------------------------------------------------
    def resolve(self) -> Tuple[int, ...]:
        """返回与逻辑元素行优先顺序对齐的物理下标元组。

        任何下标越界、或两个逻辑元素映射到同一物理位置（别名冲突）
        都会抛出 :class:`InvalidViewError`，此时整步应当作废。
        """
        if self._resolved_cache is not None:
            return self._resolved_cache

        storage_size = self._storage.size
        if self._offset >= storage_size:
            raise InvalidViewError(
                f"offset={self._offset} 越界，存储长度={storage_size}"
            )

        indices: list[int] = []
        total = self.element_count
        for flat in range(total):
            remainder = flat
            physical = self._offset
            # row优先：最后一维变化最快
            for dim in range(len(self._shape) - 1, -1, -1):
                coord = remainder % self._shape[dim]
                remainder //= self._shape[dim]
                physical += coord * self._strides[dim]
            if physical < 0 or physical >= storage_size:
                raise InvalidViewError(
                    f"逻辑位置展开序号 {flat} 映射到越界物理下标 {physical}"
                    f"（存储长度={storage_size}）"
                )
            indices.append(physical)

        if len(set(indices)) != len(indices):
            raise InvalidViewError("视图存在别名：多个逻辑位置映射到同一物理位置")

        self._resolved_cache = tuple(indices)
        return self._resolved_cache

    def __repr__(self) -> str:
        return (
            f"View(shape={self._shape!r}, strides={self._strides!r}, "
            f"offset={self._offset})"
        )
