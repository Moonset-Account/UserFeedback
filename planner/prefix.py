"""128 位整数地址空间上的前缀与区间运算。

地址一律用 Python ``int`` 表示（0 <= addr < 2**128），前缀用
``(addr, length)`` 表示，其覆盖的整数区间为
``[addr, addr + 2**(128-length))``。所有运算只操作区间端点和前缀长度，
绝不按地址数枚举，因此可以安全处理含极少固定点的巨大前缀。
"""

from __future__ import annotations

from dataclasses import dataclass

BITS = 128
SPACE_SIZE = 1 << BITS


@dataclass(frozen=True, slots=True)
class Prefix:
    """一个按位对齐的 CIDR 前缀。

    ``block_size`` 为该前缀在当前坐标系下覆盖的整数地址数：真实 IPv6 前缀
    默认为 ``2**(128-length)``；小地址投影前缀会显式传入投影块大小，使
    区间/trie 运算在投影坐标系内自洽。生产代码无需传该参数。
    """

    addr: int
    length: int
    block_size: int | None = None
    unit: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.addr, int) or isinstance(self.addr, bool):
            raise TypeError("addr 必须是 int")
        if not 0 <= self.addr < SPACE_SIZE:
            raise ValueError("addr 超出 128 位地址空间")
        if not 0 <= self.length <= BITS:
            raise ValueError("length 必须在 [0, 128]")
        natural = 1 << (BITS - self.length)
        if self.block_size is None:
            object.__setattr__(self, "block_size", natural)
        else:
            if not isinstance(self.block_size, int) or self.block_size <= 0:
                raise ValueError("block_size 必须是正整数")
            # 投影块必须是自然块的整数倍；对齐统一在末尾按坐标系检查。
            if natural > self.block_size or self.block_size % natural != 0:
                raise ValueError("block_size 必须是自然 CIDR 块大小的整数倍")
        if not (isinstance(self.unit, int) and self.unit >= 1):
            raise ValueError("unit 必须是 >=1 的整数")
        if self.unit > 1:
            # 投影坐标系：只需按投影块对齐，真实主机位允许落在投影单位内。
            if self.addr % self.block_size != 0:
                raise ValueError(f"{self.addr:#x}/{self.length} 未按投影块对齐")
        else:
            host_mask = natural - 1 if self.length < BITS else 0
            if self.addr & host_mask:
                raise ValueError(f"{self.addr:#x}/{self.length} 未按位对齐")

    # ---- 投影构造 -------------------------------------------------------
    @classmethod
    def projected(cls, addr: int, width: int, length: int) -> "Prefix":
        """把宽度 ``width`` 的投影前缀映射为自洽的 128 位坐标前缀。

        投影坐标 ``addr``（0 <= addr < 2**width）按比例放大到真实空间：

            real_addr   = addr * 2**(128-width)
            block_size  = 2**(width-length) * 2**(128-width)
                        = 2**(128-length)

        长度保留投影层级 ``length``，但用显式 ``block_size`` 表达其在投影
        坐标系下的真实跨度，从而 ``size/end/contains/sibling`` 全部与投影
        空间一一对应，且无需在生产代码里枚举地址。
        """
        if not 0 <= width <= BITS:
            raise ValueError("投影宽度必须在 [0, 128]")
        if not 0 <= length <= width:
            raise ValueError("投影长度必须在 [0, width]")
        if not 0 <= addr < (1 << width):
            raise ValueError("投影地址超出投影宽度")
        unit = 1 << (BITS - width)
        real_addr = addr * unit
        block_size = (1 << (width - length)) * unit
        return cls(real_addr, length, block_size, unit)

    # ---- 区间视图 -------------------------------------------------------
    @property
    def size(self) -> int:
        assert self.block_size is not None
        return self.block_size

    @property
    def end(self) -> int:
        """区间右端点（不含）。"""
        return self.addr + self.size

    @property
    def interval(self) -> tuple[int, int]:
        return self.addr, self.end

    # ---- 包含关系 -------------------------------------------------------
    def contains(self, other: "Prefix") -> bool:
        return self.addr <= other.addr and other.end <= self.end

    def covers_point(self, point: int) -> bool:
        return self.addr <= point < self.end

    # ---- trie 关系 ------------------------------------------------------
    @property
    def parent(self) -> "Prefix":
        if self.length == 0:
            raise ValueError("根前缀没有父节点")
        block = self.size * 2
        return Prefix((self.addr // block) * block, self.length - 1, block, self.unit)

    @property
    def sibling(self) -> "Prefix":
        if self.length == 0:
            raise ValueError("根前缀没有兄弟节点")
        if self.addr & self.size:
            return Prefix(self.addr - self.size, self.length, self.size, self.unit)
        return Prefix(self.addr + self.size, self.length, self.size, self.unit)

    def child(self, bit: int) -> "Prefix":
        if bit not in (0, 1):
            raise ValueError("bit 只能是 0 或 1")
        if self.length == BITS:
            raise ValueError("/128 没有子前缀")
        half = self.size // 2
        return Prefix(self.addr + bit * half, self.length + 1, half, self.unit)

    # ---- 渲染 -----------------------------------------------------------
    def __str__(self) -> str:
        return format_prefix(self)

    def __repr__(self) -> str:
        return f"Prefix({self.addr}, {self.length})"


# ---------------------------------------------------------------------------
# 区间工具（端点运算，不枚举地址）
# ---------------------------------------------------------------------------
def intervals_disjoint(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[1] <= b[0] or b[1] <= a[0]


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把可能重叠/相邻的区间合并为互不相交、按起点排序的区间列表。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # 相邻也可合并
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def _prefix_covers_interval(pfx: Prefix, start: int, end: int) -> bool:
    return pfx.addr <= start and end <= pfx.end


def cover_with_prefixes(
    start: int,
    end: int,
    prefixes: list[Prefix],
) -> list[Prefix]:
    """用给定前缀集合精确覆盖区间 ``[start, end)``，返回最小前缀分解。

    先校验给定前缀的并集完整包含目标区间，再对区间做规范 CIDR 分解。
    复杂度只与输出前缀条数相关，与地址数无关；覆盖不足时抛 ``ValueError``。
    """
    if start >= end:
        return []
    # 延迟导入避免与 aggregate 形成循环依赖。
    from .aggregate import decompose_interval

    covered = merge_intervals(
        [p.interval for p in prefixes if not intervals_disjoint(p.interval, (start, end))]
    )
    cursor = start
    for iv_start, iv_end in covered:
        if iv_start > cursor:
            break
        cursor = max(cursor, iv_end)
        if cursor >= end:
            break
    if cursor < end:
        raise ValueError(f"区间 [{start:#x}, {end:#x}) 未被给定前缀集合完整覆盖")
    return decompose_interval(start, end)


def union_covers(prefixes: list[Prefix], target: Prefix) -> bool:
    """判断 ``prefixes`` 的区间并集是否完整覆盖 ``target``（纯端点运算）。"""
    covered = merge_intervals([p.interval for p in prefixes if not intervals_disjoint(p.interval, target.interval)])
    cursor = target.addr
    for start, end in covered:
        if start > cursor:
            return False
        if end > cursor:
            cursor = end
        if cursor >= target.end:
            return True
    return cursor >= target.end


def parse_prefix(text: str) -> Prefix:
    """解析 ``"xxxx:..../len"`` 形式的 IPv6 前缀（基于标准库 ipaddress）。"""
    import ipaddress

    net = ipaddress.IPv6Network(text, strict=True)
    return Prefix(int(net.network_address), net.prefixlen)


def format_prefix(pfx: Prefix) -> str:
    import ipaddress

    return f"{ipaddress.IPv6Address(pfx.addr)}/{pfx.length}"
