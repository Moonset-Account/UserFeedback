"""测试共用的小地址投影工厂。

所有地址都放在宽度 W=8（共 256 个投影地址）的空间里，前缀长度用
``sl(a, bits)`` 构造，其中 bits 为该前缀占的地址位数（长度 = W-bits）。
这些工具只服务测试，生产代码不依赖投影。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner.model import Prefix, TempPrefix, Tenant, Upstream  # noqa: E402

W = 8


def sl(addr: int, bits: int) -> Prefix:
    """占 2**bits 个投影地址、起点为 addr 的对齐前缀（投影坐标系自洽）。"""
    return Prefix.projected(addr, W, W - bits)


def make_upstream(name: str, addr: int, bits: int, cap: int) -> Upstream:
    return Upstream(name, sl(addr, bits), cap)


def make_tenant(
    name: str,
    bits: int,
    *,
    old: tuple[int, int] | None = None,
    old_upstream: str | None = None,
    fixed: tuple[tuple[int, int], ...] = (),
    hold: int = 1,
    allowed: tuple[str, ...] = (),
    policy: str = "default",
) -> Tenant:
    fixed_pfxs = tuple(sl(a, b) for a, b in fixed)
    old_pfx = sl(old[0], old[1]) if old is not None else None
    return Tenant(
        name=name,
        prefix_length=W - bits,
        fixed=fixed_pfxs,
        allowed_upstreams=allowed,
        old_prefix=old_pfx,
        old_upstream=old_upstream,
        hold_rounds=hold,
        policy=policy,
    )


def make_temp(name: str, addr: int, bits: int, upstream: str) -> TempPrefix:
    return TempPrefix(name, sl(addr, bits), upstream)
