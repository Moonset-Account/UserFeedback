"""IEEE CRC32 的无字节组合（纯 Python，仅依赖标准库 :mod:`zlib`）。

核心能力
========
:func:`crc32_combine` 仅凭两段各自的 CRC32 与第二段长度，就能得到两段
拼接后的 CRC32，而不需要任何一段的原始字节::

    crc32_combine(zlib.crc32(A), zlib.crc32(B), len(B)) == zlib.crc32(A + B)

数学背景
========
CRC32 在固定初值（全 1 寄存器）与固定反馈多项式 0xEDB88320 下，对“追加
字节”构成一个仿射变换。把它拆成纯线性部分 ``L`` 与常数项后，线性部分是
GF(2) 上的 32x32 矩阵，支持二进制幂：追加 ``n`` 个字节对应算子
``T^n``，可由单字节算子逐次平方（GF(2) 矩阵平方）后按 ``n`` 的二进制
位施加。

实现约定
========
- 矩阵表示为 32 个 32 位整数行（位掩码），与 zlib 的 ``uLong[32]`` 同构；
- 所有 GF(2) 运算结果显式按 32 位掩码截断（Python 整数为任意精度，
  不会像 C 的 ``unsigned long`` 那样自动截断）；
- :mod:`zlib` 的 :func:`zlib.crc32` 返回值是“外部 CRC”，与内部寄存器
  状态相差一次按位取反；组合在内部状态域进行，进出各做一次取反；
- CRC 寄存器初值为全 1（``0xFFFFFFFF``），第二段的线性部分为
  ``L(B) = i(B) ^ T^lenB(0xFFFFFFFF)``。

正确性由 tests/test_crc32.py 中上万次随机切分对照 :func:`zlib.crc32`
的全量计算保证。
"""

from __future__ import annotations

import zlib

GF2_DIM = 32
_MASK32 = 0xFFFFFFFF
# CRC32 寄存器初值（全 1）
_CRC_INIT = _MASK32
# CRC32 反馈多项式（反射形式）
_CRC_POLY = 0xEDB88320


def _gf2_matrix_times(mat: list[int], vec: int) -> int:
    """GF(2) 矩阵乘向量：``vec`` 的置位位选中矩阵对应行掩码做异或。"""
    total = 0
    i = 0
    while vec:
        if vec & 1:
            total ^= mat[i]
        vec >>= 1
        i += 1
    return total & _MASK32


def _gf2_matrix_square(square: list[int], mat: list[int]) -> None:
    """GF(2) 矩阵平方，结果写入 ``square``（就地）。

    利用 ``(M^2)[n][k] = XOR_j M[n][j] * M[j][k]``：遍历第 ``n`` 行的
    每个置位位 ``j``，把第 ``j`` 行整行异或进来。Python 整数无宽度，
    最后显式 32 位截断。
    """
    for n in range(GF2_DIM):
        row = 0
        bits = mat[n]
        while bits:
            low = bits & -bits
            bits ^= low
            j = low.bit_length() - 1
            row ^= mat[j]
        square[n] = row & _MASK32


def _build_byte_table() -> list[int]:
    """生成标准 CRC32 字节查表（256 项）。"""
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            if c & 1:
                c = (c >> 1) ^ _CRC_POLY
            else:
                c >>= 1
        table.append(c & _MASK32)
    return table


# 单字节转移表（模块级只构造一次）
_BYTE_TABLE = _build_byte_table()


def _step_byte(state: int) -> int:
    """内部 CRC 状态追加一个字节后的转移。"""
    return ((state >> 8) ^ _BYTE_TABLE[state & 0xFF]) & _MASK32


def _build_byte_operator() -> list[int]:
    """构造单字节线性算子 T：第 n 行 = T 作用在单位向量 e_n 上的结果。"""
    return [_step_byte(1 << n) for n in range(GF2_DIM)]


_BYTE_OPERATOR = _build_byte_operator()


def _apply_power(state: int, nbytes: int) -> int:
    """把算子 T^nbytes 作用到内部状态 ``state``（二进制幂）。"""
    power = _BYTE_OPERATOR
    n = nbytes
    while n:
        if n & 1:
            state = _gf2_matrix_times(power, state)
        n >>= 1
        if n:
            nxt = [0] * GF2_DIM
            _gf2_matrix_square(nxt, power)
            power = nxt
    return state & _MASK32


def crc32_combine(crc1: int, crc2: int, len2: int) -> int:
    """合并两段字节流的 CRC32。

    等价于 ``zlib.crc32(bytes1 + bytes2)``，其中
    ``crc1 == zlib.crc32(bytes1)``、``crc2 == zlib.crc32(bytes2)``、
    ``len2 == len(bytes2)``，但无需持有任一段的原始字节。

    ``len2 == 0`` 时恒等返回 ``crc1``（空段不改变校验值）。
    """
    if len2 == 0:
        return crc1
    if len2 < 0:
        raise ValueError("len2 必须为非负整数")

    # 外部 CRC -> 内部寄存器状态（初值全 1，进入时按位取反）
    internal1 = (~crc1) & _MASK32
    internal2 = (~crc2) & _MASK32

    # T^len2 作用于第一段内部状态
    shifted1 = _apply_power(internal1, len2)
    # 第二段的纯线性部分 L(B) = i(B) ^ T^len2(初始寄存器)
    shifted_init = _apply_power(_CRC_INIT, len2)
    linear2 = internal2 ^ shifted_init

    out_internal = (shifted1 ^ linear2) & _MASK32
    # 内部状态 -> 外部 CRC（退出时再取反）
    return (~out_internal) & _MASK32


def crc32_zero_extend(crc: int, nzeros: int) -> int:
    """在已有 CRC32 对应的字节后追加 ``nzeros`` 个零字节后的 CRC32。

    零字节也是真实字节，直接对内部状态施加 T^nzeros 即可，
    不需要第二段 CRC，也不需要分配零字节缓冲。
    """
    if nzeros < 0:
        raise ValueError("nzeros 必须为非负整数")
    if nzeros == 0:
        return crc
    internal = (~crc) & _MASK32
    out_internal = _apply_power(internal, nzeros)
    return (~out_internal) & _MASK32


# 零块 CRC 计算时使用的缓冲大小
_ZERO_BUFFER_SIZE = 64 * 1024


def crc32_zeros(n: int) -> int:
    """``zlib.crc32(b"\\x00" * n)``，采用固定缓冲循环，避免分配大对象。"""
    if n < 0:
        raise ValueError("n 必须为非负整数")
    crc = 0
    block = b"\x00" * min(n, _ZERO_BUFFER_SIZE)
    remaining = n
    while remaining:
        size = min(remaining, _ZERO_BUFFER_SIZE)
        crc = zlib.crc32(block[:size], crc)
        remaining -= size
    return crc
