"""GF(65537) 阈值重建与错误纠正核心实现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Dict, List, Optional, Sequence, Tuple

#: 批次描述固定使用的素数（GF(p)）。
FIELD_PRIME = 65537

#: 支持的最大份额数量。
MAX_SHARES = 31

#: 支持的最大原文字节数。
MAX_BYTES = 4096


class RecoveryStatus(str, Enum):
    """重建结果分类。"""

    OK = "ok"
    #: 证据不足：剔除缺失/不可用项后达不到 t+2e 个不同横坐标。
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    #: 不一致：批次混入、重复坐标异值冲突、超出纠错能力或校验失败。
    INCONSISTENT = "inconsistent"


class InconsistencyReason(str, Enum):
    """不一致结果的机器可读原因码（不含任何秘密信息）。"""

    BATCH_MISMATCH = "batch_mismatch"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    MALFORMED_SHARE = "malformed_share"
    UNKNOWN_SHARE_POSITION = "unknown_share_position"
    TOO_MANY_ERRORS = "too_many_errors"
    DECODE_FAILURE = "decode_failure"
    CONSTANT_NOT_BYTE = "constant_not_byte"
    DIGEST_MISMATCH = "digest_mismatch"
    INVALID_DESCRIPTOR = "invalid_descriptor"


@dataclass(frozen=True)
class BatchDescriptor:
    """可信批次描述。

    :param batch_id: 批次标识，份额必须携带同一批次 ID。
    :param prime: 有限域素数，当前固定为 65537。
    :param threshold: 门限 t，每列多项式次数小于 t。
    :param share_xs: 各份额编号对应的横坐标，份额编号即该列表的下标。
    :param original_length: 原始字节长度。
    :param sha256: 原文 SHA-256 摘要（bytes 或十六进制字符串）。
    """

    batch_id: str
    prime: int
    threshold: int
    share_xs: Sequence[int]
    original_length: int
    sha256: bytes

    def __post_init__(self) -> None:
        if self.prime != FIELD_PRIME:
            raise ValueError("only prime 65537 is supported")
        if not isinstance(self.threshold, int) or self.threshold < 1:
            raise ValueError("threshold must be a positive integer")
        if not 1 <= self.original_length <= MAX_BYTES:
            raise ValueError(
                f"original_length must be within 1..{MAX_BYTES}"
            )
        xs = list(self.share_xs)
        if not (1 <= len(xs) <= MAX_SHARES):
            raise ValueError(f"share count must be within 1..{MAX_SHARES}")
        if len(set(xs)) != len(xs):
            raise ValueError("share_xs must be distinct")
        if any(not isinstance(x, int) or not (1 <= x < self.prime) for x in xs):
            raise ValueError("share xs must be distinct field elements in 1..p-1")
        if self.threshold > len(xs):
            raise ValueError("threshold cannot exceed share count")
        digest = self.sha256
        if isinstance(digest, str):
            try:
                digest = bytes.fromhex(digest)
            except ValueError as exc:
                raise ValueError("sha256 hex string is invalid") from exc
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("sha256 must be 32 bytes (or a 64-char hex string)")
        # frozen dataclass 上回写规范化后的摘要。
        object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True)
class Share:
    """输入份额。

    :param batch_id: 份额声称所属的批次 ID。
    :param number: 份额编号（对应 ``BatchDescriptor.share_xs`` 的下标）。
    :param values: 各列纵坐标，长度等于原文字节数；``None`` 表示整份缺失。
                   列表中单个元素为 ``None`` 表示该列缺失。
    """

    batch_id: str
    number: int
    values: Optional[Sequence[Optional[int]]]


@dataclass
class RecoveryResult:
    """重建结果。失败时 ``secret`` 恒为 ``None``，不返回部分秘密。"""

    status: RecoveryStatus
    reason: Optional[InconsistencyReason] = None
    secret: Optional[bytes] = None
    #: 诊断只包含份额编号；非冲突类失败可能为空列表。
    diagnostics: List[int] = field(default_factory=list)
    #: 被去重接受的重复份额编号（同坐标同值）。
    deduplicated: List[int] = field(default_factory=list)
    error_positions: List[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# GF(p) 基础运算
# ---------------------------------------------------------------------------


def _inv(a: int, p: int = FIELD_PRIME) -> int:
    """扩展欧几里得求逆；a 为 0 时抛 ZeroDivisionError。"""
    a %= p
    if a == 0:
        raise ZeroDivisionError("inverse of zero")
    old_r, r = a, p
    old_s, s = 1, 0
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_s, s = s, old_s - q * s
    return old_s % p


def _poly_eval(coeffs: Sequence[int], x: int, p: int = FIELD_PRIME) -> int:
    """Horner 法求多项式值，coeffs[i] 为 x^i 的系数。"""
    acc = 0
    for c in reversed(coeffs):
        acc = (acc * x + c) % p
    return acc


def _poly_divmod(
    dividend: Sequence[int],
    divisor: Sequence[int],
    p: int = FIELD_PRIME,
) -> Tuple[List[int], List[int]]:
    """域上多项式带余除法，返回 (商, 余数)；输入按低次到高次排列。"""
    out = [c % p for c in dividend]
    div = [c % p for c in divisor]
    while div and div[-1] == 0:
        div.pop()
    if not div:
        raise ZeroDivisionError("polynomial division by zero")
    degree_div = len(div) - 1
    quotient = [0] * max(0, len(out) - len(div) + 1)
    inv_lead = _inv(div[-1], p)
    while len(out) - 1 >= degree_div and not (len(out) == 1 and out[0] == 0):
        shift = len(out) - 1 - degree_div
        factor = (out[-1] * inv_lead) % p
        quotient[shift] = factor
        for i in range(len(div)):
            out[shift + i] = (out[shift + i] - factor * div[i]) % p
        while len(out) > 1 and out[-1] == 0:
            out.pop()
    return quotient, out


def _solve_linear(
    matrix: List[List[int]],
    rhs: List[int],
    p: int = FIELD_PRIME,
) -> Optional[List[int]]:
    """高斯-约当消元解 GF(p) 线性方程组。

    矩阵奇异或方程组无解时返回 ``None``。
    """
    n_rows = len(matrix)
    n_cols = len(matrix[0]) if n_rows else 0
    aug = [row[:] + [rhs[i] % p] for i, row in enumerate(matrix)]
    pivot_row = 0
    pivot_cols: List[int] = []
    for col in range(n_cols):
        pivot = next(
            (r for r in range(pivot_row, n_rows) if aug[r][col] % p != 0),
            None,
        )
        if pivot is None:
            continue
        aug[pivot_row], aug[pivot] = aug[pivot], aug[pivot_row]
        inv_pivot = _inv(aug[pivot_row][col], p)
        aug[pivot_row] = [(v * inv_pivot) % p for v in aug[pivot_row]]
        for r in range(n_rows):
            if r != pivot_row and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [
                    (a - factor * b) % p for a, b in zip(aug[r], aug[pivot_row])
                ]
        pivot_cols.append(col)
        pivot_row += 1
        if pivot_row == n_rows:
            break
    # 检查 0 = 非零 的矛盾行。
    for r in range(pivot_row, n_rows):
        if aug[r][n_cols] != 0:
            return None
    solution = [0] * n_cols
    for row_idx, col in zip(range(len(pivot_cols)), pivot_cols):
        solution[col] = aug[row_idx][n_cols]
    return solution


# ---------------------------------------------------------------------------
# Berlekamp-Welch 纠错
# ---------------------------------------------------------------------------


def berlekamp_welch(
    points: Sequence[Tuple[int, int]],
    t: int,
    e: int,
    p: int = FIELD_PRIME,
) -> Optional[Tuple[List[int], List[int]]]:
    """Berlekamp-Welch 重建次数小于 t 的消息多项式。

    :param points: ``(横坐标, 纵坐标)`` 对，调用方保证横坐标互不相同，
                   且点数 n 满足 n >= t + 2e。
    :param t: 消息多项式次数上界（次数 < t）。
    :param e: 调用方声明的最大错误位置数。
    :returns: ``(消息多项式系数低次到高次, 错误定位多项式系数)``；
              无法在该 e 下一致解码时返回 ``None``。
    """
    n = len(points)
    if n < t + 2 * e:
        return None

    # 未知量布局：Q = q_0..q_{t+e-1}（共 t+e 个），E = e_0..e_{e-1}（共 e 个），
    # 且 E 的首项系数固定为 1（monic）。
    n_q = t + e
    n_unknown = n_q + e
    matrix: List[List[int]] = []
    rhs: List[int] = []
    for x, y in points:
        row = [0] * n_unknown
        x_pow = 1
        for k in range(n_q):
            row[k] = x_pow
            x_pow = (x_pow * x) % p
        x_pow = 1
        for k in range(e):
            row[n_q + k] = (-y * x_pow) % p
            x_pow = (x_pow * x) % p
        matrix.append(row)
        # Q(x_i) = y_i * x_i^e - sum(e_k * y_i * x_i^k)
        rhs.append((y * pow(x, e, p)) % p)

    sol = _solve_linear(matrix, rhs, p)
    if sol is None:
        return None
    q_coeffs = [sol[k] % p for k in range(n_q)]
    e_coeffs = [sol[n_q + k] % p for k in range(e)] + [1]

    # 去掉高位零，检查 E 是否退化为零多项式。
    while len(e_coeffs) > 1 and e_coeffs[-1] == 0:
        e_coeffs.pop()
    if len(e_coeffs) == 1 and e_coeffs[0] == 0:
        return None

    msg, remainder = _poly_divmod(q_coeffs, e_coeffs, p)
    if any(c % p != 0 for c in remainder):
        return None
    while len(msg) > 1 and msg[-1] == 0:
        msg.pop()
    if len(msg) > t:
        return None
    return msg, e_coeffs


# ---------------------------------------------------------------------------
# 重建入口
# ---------------------------------------------------------------------------


def _fail(
    status: RecoveryStatus,
    reason: Optional[InconsistencyReason],
    diagnostics: Sequence[int] = (),
) -> RecoveryResult:
    return RecoveryResult(
        status=status,
        reason=reason,
        secret=None,
        diagnostics=sorted(set(diagnostics)),
    )


def recover(
    descriptor: BatchDescriptor,
    shares: Sequence[Share],
    max_errors: int,
) -> RecoveryResult:
    """按批次描述重建原始字节。

    任何失败路径都不会返回部分秘密：``secret`` 恒为 ``None``。
    诊断列表中只出现份额编号。
    """
    if not isinstance(descriptor, BatchDescriptor):
        return _fail(
            RecoveryStatus.INCONSISTENT,
            InconsistencyReason.INVALID_DESCRIPTOR,
        )
    if not isinstance(max_errors, int) or max_errors < 0:
        raise ValueError("max_errors must be a non-negative integer")

    p = descriptor.prime
    t = descriptor.threshold
    length = descriptor.original_length
    x_by_number = {idx: x for idx, x in enumerate(descriptor.share_xs)}

    # ---- 1. 结构与批次校验 ------------------------------------------------
    # 同一份额编号可能被提交多份副本（同一横坐标的重复观测）：
    # number -> 该编号下所有非缺失副本的纵坐标列表。
    submissions: Dict[int, List[List[Optional[int]]]] = {}
    for share in shares:
        if not isinstance(share, Share) or not isinstance(share.number, int):
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.MALFORMED_SHARE,
            )
        number = share.number
        if number not in x_by_number:
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.UNKNOWN_SHARE_POSITION,
                [number],
            )
        if share.batch_id != descriptor.batch_id:
            # 批次混入必须显式失败，不能靠插值忽略。
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.BATCH_MISMATCH,
                [number],
            )
        if share.values is None:
            continue  # 整份缺失：直接剔除。
        values = list(share.values)
        if len(values) != length or any(
            v is not None and (not isinstance(v, int) or not (0 <= v < p))
            for v in values
        ):
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.MALFORMED_SHARE,
                [number],
            )
        submissions.setdefault(number, []).append(values)

    # ---- 2. 同坐标重复副本：同值去重，异值冲突 ----------------------------
    # x -> (规范份额编号, 规范纵坐标)。
    grouped: Dict[int, Tuple[int, List[Optional[int]]]] = {}
    deduplicated: List[int] = []
    for number, copies in submissions.items():
        canonical = copies[0]
        for extra in copies[1:]:
            if extra != canonical:
                return _fail(
                    RecoveryStatus.INCONSISTENT,
                    InconsistencyReason.DUPLICATE_CONFLICT,
                    [number],
                )
            deduplicated.append(number)
        grouped[x_by_number[number]] = (number, canonical)

    column_points: List[Dict[int, Tuple[int, int]]] = [
        {} for _ in range(length)
    ]
    for x, (number, canonical) in grouped.items():
        for col, v in enumerate(canonical):
            if v is not None:  # 单列缺失同样剔除。
                column_points[col][x] = (number, v)

    distinct_x = len(grouped)
    if distinct_x < t + 2 * max_errors:
        return _fail(
            RecoveryStatus.INSUFFICIENT_EVIDENCE,
            None,
        )

    # ---- 3. 逐列 Berlekamp-Welch 纠错重建 --------------------------------
    column_polys: List[List[int]] = []
    error_union: set[int] = set()

    for col, points_map in enumerate(column_points):
        if len(points_map) < t + 2 * max_errors:
            return _fail(
                RecoveryStatus.INSUFFICIENT_EVIDENCE,
                None,
            )
        xs = sorted(points_map)
        points = [(x, points_map[x][1]) for x in xs]
        decoded = berlekamp_welch(points, t, max_errors, p)
        if decoded is None:
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.DECODE_FAILURE,
            )
        msg, locator = decoded

        # 用恢复出的多项式重新核对全部已提供点，定位错误份额编号。
        mismatched: List[int] = []
        for x in xs:
            number, y = points_map[x]
            if _poly_eval(msg, x, p) != y:
                mismatched.append(number)
        if len(mismatched) > max_errors:
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.TOO_MANY_ERRORS,
                mismatched,
            )
        # 错误定位多项式必须恰好在错误点取零、在正确点非零。
        for x in xs:
            at_zero = _poly_eval(locator, x, p) == 0
            number, _ = points_map[x]
            if at_zero != (number in mismatched):
                return _fail(
                    RecoveryStatus.INCONSISTENT,
                    InconsistencyReason.DECODE_FAILURE,
                )
        error_union.update(mismatched)
        column_polys.append(msg)

    # ---- 4. 全列联合校验：错误位置并集上限 e ------------------------------
    if len(error_union) > max_errors:
        return _fail(
            RecoveryStatus.INCONSISTENT,
            InconsistencyReason.TOO_MANY_ERRORS,
            sorted(error_union),
        )

    # ---- 5. 常数项必须可表示字节 ------------------------------------------
    secret_bytes = bytearray()
    for msg in column_polys:
        constant = msg[0] if msg else 0
        if not 0 <= constant <= 255:
            return _fail(
                RecoveryStatus.INCONSISTENT,
                InconsistencyReason.CONSTANT_NOT_BYTE,
            )
        secret_bytes.append(constant)

    # ---- 6. 原文摘要校验 --------------------------------------------------
    digest = hashlib.sha256(bytes(secret_bytes)).digest()
    if digest != descriptor.sha256:
        return _fail(
            RecoveryStatus.INCONSISTENT,
            InconsistencyReason.DIGEST_MISMATCH,
        )

    return RecoveryResult(
        status=RecoveryStatus.OK,
        reason=None,
        secret=bytes(secret_bytes),
        diagnostics=[],
        deduplicated=sorted(deduplicated),
        error_positions=sorted(error_union),
    )
