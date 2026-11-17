#!/usr/bin/env python3
"""本地核验入口：直接调用 secret_recovery 并执行真实断言。

用法：
    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case all

任一断言失败、入口缺失、未实现、零断言或整组跳过均以非零码退出。
输出为单条 JSON 记录，不含凭据、原始敏感样本或私有账户内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence

# 以 -I 运行时没有 sys.path 注入，显式加入工作区根目录。
_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from secret_recovery import (  # noqa: E402
    FIELD_PRIME,
    MAX_BYTES,
    MAX_SHARES,
    BatchDescriptor,
    InconsistencyReason,
    RecoveryStatus,
    Share,
    recover,
)
from secret_recovery.recovery import _poly_eval  # noqa: E402


# ---------------------------------------------------------------------------
# 确定性本地测试脚手架（固定多项式，不使用任何外部服务）
# ---------------------------------------------------------------------------


class LcgRng:
    """固定种子的纯软件 LCG，保证样本可重放。"""

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFF

    def next_u32(self) -> int:
        # Numerical Recipes 常数。
        self._state = (1664525 * self._state + 1013904223) & 0xFFFFFFFF
        return self._state

    def below(self, n: int) -> int:
        return self.next_u32() % n


def make_secret(rng: LcgRng, length: int) -> bytes:
    return bytes(rng.below(256) for _ in range(length))


def make_shares(
    secret: bytes,
    t: int,
    n: int,
    rng: LcgRng,
    xs: Optional[Sequence[int]] = None,
) -> List[List[int]]:
    """每列独立采样次数 < t 的多项式，常数项为该字节；返回 n 份份额。"""
    if xs is None:
        xs = list(range(1, n + 1))
    shares: List[List[int]] = [[] for _ in range(n)]
    for byte in secret:
        coeffs = [byte] + [rng.below(FIELD_PRIME) for _ in range(t - 1)]
        for i, x in enumerate(xs):
            shares[i].append(_poly_eval(coeffs, x))
    return shares


def make_descriptor(
    secret: bytes,
    t: int,
    n: int,
    batch_id: str = "batch-local",
    xs: Optional[Sequence[int]] = None,
) -> BatchDescriptor:
    if xs is None:
        xs = list(range(1, n + 1))
    return BatchDescriptor(
        batch_id=batch_id,
        prime=FIELD_PRIME,
        threshold=t,
        share_xs=list(xs),
        original_length=len(secret),
        sha256=hashlib.sha256(secret).digest(),
    )


def corrupt(
    shares: List[List[int]],
    numbers: Sequence[int],
    rng: LcgRng,
) -> None:
    """对指定份额的每一列写入一个确定的不同错误值。"""
    for number in numbers:
        for col in range(len(shares[number])):
            bad = (shares[number][col] + 1 + rng.below(FIELD_PRIME - 1)) % FIELD_PRIME
            shares[number][col] = bad


# ---------------------------------------------------------------------------
# 断言记录器
# ---------------------------------------------------------------------------


class CheckRecorder:
    def __init__(self, case_id: str, seed: int, summary: str) -> None:
        self.case_id = case_id
        self.seed = seed
        self.summary = summary
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: List[str] = []
        self.observations: List[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(label + (f": {detail}" if detail else ""))

    def note(self, text: str) -> None:
        self.observations.append(text)

    def record(self) -> Dict[str, object]:
        return {
            "case": self.case_id,
            "seed": self.seed,
            "summary": self.summary,
            "assertions": {
                "total": self.passed + self.failed + self.skipped,
                "passed": self.passed,
                "failed": self.failed,
                "skipped": self.skipped,
            },
            "failures": self.failures,
            "observations": self.observations,
        }


# ---------------------------------------------------------------------------
# C01：固定本地多项式 / 域 / t+2e 门限 / 正确字节恢复
# ---------------------------------------------------------------------------


def case_c01() -> CheckRecorder:
    seed = 2026111801
    rng = LcgRng(seed)
    t, n, e, length = 3, 7, 2, 16
    secret = make_secret(rng, length)
    raw = make_shares(secret, t, n, rng)
    desc = make_descriptor(secret, t, n)
    rec = CheckRecorder(
        "C01",
        seed,
        f"t={t}, n={n}, e={e}, bytes={length}; 域/门限/字节恢复",
    )

    rec.check("field prime is 65537", FIELD_PRIME == 65537)
    rec.check("descriptor prime is 65537", desc.prime == 65537)
    rec.check("all y values inside GF(p)", all(0 <= v < FIELD_PRIME for row in raw for v in row))

    shares = [Share("batch-local", i, raw[i]) for i in range(n)]

    # 门限边界：n = t+2e 恰好满足。
    rec.check("n equals t+2e boundary", n == t + 2 * e)
    ok = recover(desc, shares, e)
    rec.check("boundary reconstruction OK", ok.status == RecoveryStatus.OK, str(ok.status))
    rec.check("recovered bytes equal original", ok.secret == secret)
    rec.check("no error positions reported", ok.error_positions == [])
    rec.check("diagnostics empty on success", ok.diagnostics == [])

    # 少一份（缺失后只剩 t+2e-1 个不同横坐标）必须判证据不足，且不返回秘密。
    missing = shares[:-1]
    short = recover(desc, missing, e)
    rec.check(
        "t+2e-1 distinct x => insufficient evidence",
        short.status == RecoveryStatus.INSUFFICIENT_EVIDENCE,
        str(short.status),
    )
    rec.check("insufficient result leaks no secret", short.secret is None)

    # 声明更小的 e 时门限放宽，同样可恢复。
    ok0 = recover(desc, shares, 0)
    rec.check("e=0 clean reconstruction OK", ok0.status == RecoveryStatus.OK)
    rec.check("e=0 recovered bytes equal original", ok0.secret == secret)
    return rec


# ---------------------------------------------------------------------------
# C02：缺失 / 重复同值去重 / 重复异值冲突 / 批次混入
# ---------------------------------------------------------------------------


def case_c02() -> CheckRecorder:
    seed = 2026111802
    rng = LcgRng(seed)
    t, n, e, length = 3, 8, 1, 20
    secret = make_secret(rng, length)
    raw = make_shares(secret, t, n, rng)
    desc = make_descriptor(secret, t, n)
    rec = CheckRecorder(
        "C02",
        seed,
        f"t={t}, n={n}, e={e}, bytes={length}; 缺失/去重/冲突/批次混入",
    )

    # 1) 整份缺失 + 单列缺失：剩余不同横坐标仍 >= t+2e。
    shares: List[Share] = []
    for i in range(n):
        if i == 7:
            shares.append(Share("batch-local", i, None))  # 整份缺失
        else:
            vals = list(raw[i])
            if i == 6:
                vals[0] = None  # 单列缺失
            shares.append(Share("batch-local", i, vals))
    missing_result = recover(desc, shares, e)
    rec.check(
        "missing shares still reconstruct",
        missing_result.status == RecoveryStatus.OK,
        str(missing_result.status),
    )
    rec.check("missing-case bytes equal original", missing_result.secret == secret)
    rec.check("missing-case secret is not partial", len(missing_result.secret or b"") == length)

    # 2) 重复坐标同值去重：份额 2 再提交一份完全相同的副本。
    dup = shares + [Share("batch-local", 2, list(raw[2]))]
    dedup_result = recover(desc, dup, e)
    rec.check(
        "duplicate same-value accepted",
        dedup_result.status == RecoveryStatus.OK,
        str(dedup_result.status),
    )
    rec.check("dedup reports share number", dedup_result.deduplicated == [2])
    rec.check("dedup reconstruction correct", dedup_result.secret == secret)

    # 3) 重复坐标异值冲突：同一份额编号给出不同纵坐标。
    conflict_values = list(raw[2])
    conflict_values[5] = (conflict_values[5] + 777) % FIELD_PRIME
    conflict_input = shares + [Share("batch-local", 2, conflict_values)]
    conflict_result = recover(desc, conflict_input, e)
    rec.check(
        "duplicate differing values => inconsistent",
        conflict_result.status == RecoveryStatus.INCONSISTENT,
        str(conflict_result.status),
    )
    rec.check(
        "conflict reason is duplicate_conflict",
        conflict_result.reason == InconsistencyReason.DUPLICATE_CONFLICT,
    )
    rec.check("conflict diagnostics only share numbers", conflict_result.diagnostics == [2])
    rec.check("conflict leaks no secret", conflict_result.secret is None)

    # 4) 批次混入：份额 4 携带外来批次 ID，必须显式失败。
    mixed = list(shares)
    mixed[4] = Share("foreign-batch", 4, list(raw[4]))
    mix_result = recover(desc, mixed, e)
    rec.check(
        "foreign batch => inconsistent",
        mix_result.status == RecoveryStatus.INCONSISTENT,
        str(mix_result.status),
    )
    rec.check(
        "batch reason is batch_mismatch",
        mix_result.reason == InconsistencyReason.BATCH_MISMATCH,
    )
    rec.check("batch diagnostics only share number", mix_result.diagnostics == [4])
    rec.check("batch mismatch leaks no secret", mix_result.secret is None)

    # 5) 未知份额编号（超出描述横坐标范围）。
    unknown = shares + [Share("batch-local", 99, list(raw[0]))]
    unk_result = recover(desc, unknown, e)
    rec.check(
        "unknown share position => inconsistent",
        unk_result.status == RecoveryStatus.INCONSISTENT,
        str(unk_result.status),
    )
    rec.check(
        "unknown reason is unknown_share_position",
        unk_result.reason == InconsistencyReason.UNKNOWN_SHARE_POSITION,
    )
    rec.check("unknown diagnostics only share number", unk_result.diagnostics == [99])
    return rec


# ---------------------------------------------------------------------------
# C03：错误份额位置 / e 上限 / 字节常数范围 / 原文 SHA-256
# ---------------------------------------------------------------------------


def case_c03() -> CheckRecorder:
    seed = 2026111803
    rng = LcgRng(seed)
    t, n, e, length = 4, 10, 2, 32
    secret = make_secret(rng, length)
    raw = make_shares(secret, t, n, rng)
    desc = make_descriptor(secret, t, n)
    rec = CheckRecorder(
        "C03",
        seed,
        f"t={t}, n={n}, e={e}, bytes={length}; 错误位置/e上限/常数/摘要",
    )

    # 共同的错误份额位置：1 与 6（所有列均被抄写错误）。
    bad_numbers = [1, 6]
    corrupt(raw, bad_numbers, rng)
    shares = [Share("batch-local", i, raw[i]) for i in range(n)]

    result = recover(desc, shares, e)
    rec.check("corrupted reconstruction OK", result.status == RecoveryStatus.OK, str(result.status))
    rec.check("recovered bytes equal original", result.secret == secret)
    rec.check(
        "error positions match common bad shares",
        result.error_positions == bad_numbers,
        str(result.error_positions),
    )
    rec.check("error count within e", len(result.error_positions) <= e)
    rec.check("all constants are bytes", all(0 <= b <= 255 for b in (result.secret or b"")))
    rec.check(
        "digest matches descriptor sha256",
        hashlib.sha256(result.secret or b"").digest() == desc.sha256,
    )

    # 同样的数据但只声明 e=1：超出纠错能力，必须不一致且不返回秘密。
    tight = recover(desc, shares, 1)
    rec.check(
        "e=1 with 2 bad shares => inconsistent",
        tight.status == RecoveryStatus.INCONSISTENT,
        str(tight.status),
    )
    rec.check("over-capacity result leaks no secret", tight.secret is None)
    rec.check(
        "over-capacity reason is decode/too-many",
        tight.reason
        in (InconsistencyReason.DECODE_FAILURE, InconsistencyReason.TOO_MANY_ERRORS),
        str(tight.reason),
    )
    rec.check("diagnostics contain share numbers only", all(isinstance(x, int) for x in tight.diagnostics))

    # 常数项越界：手工构造一个次数 < t、常数项为 256 的列（第 0 列）。
    evil = make_shares(secret, t, n, LcgRng(seed ^ 0x5A5A))
    evil_coeffs = [256, 11, 222, 333][:t]
    for i, x in enumerate(desc.share_xs):
        evil[i][0] = _poly_eval(evil_coeffs, x)
    evil_desc = BatchDescriptor(
        batch_id="batch-local",
        prime=FIELD_PRIME,
        threshold=t,
        share_xs=list(desc.share_xs),
        original_length=length,
        sha256=hashlib.sha256(secret).digest(),  # 故意保留原摘要
    )
    evil_shares = [Share("batch-local", i, evil[i]) for i in range(n)]
    evil_result = recover(evil_desc, evil_shares, 0)
    rec.check(
        "constant 256 => inconsistent",
        evil_result.status == RecoveryStatus.INCONSISTENT,
        str(evil_result.status),
    )
    rec.check(
        "constant-out-of-range reason",
        evil_result.reason == InconsistencyReason.CONSTANT_NOT_BYTE,
        str(evil_result.reason),
    )
    rec.check("out-of-range result leaks no secret", evil_result.secret is None)

    # 摘要不一致：正确恢复但描述给了错误 SHA-256。
    wrong_digest = hashlib.sha256(secret + b"x").digest()
    bad_hash_desc = BatchDescriptor(
        batch_id="batch-local",
        prime=FIELD_PRIME,
        threshold=t,
        share_xs=list(desc.share_xs),
        original_length=length,
        sha256=wrong_digest,
    )
    clean_raw = make_shares(secret, t, n, LcgRng(seed ^ 0x0F0F))
    hash_result = recover(
        bad_hash_desc,
        [Share("batch-local", i, clean_raw[i]) for i in range(n)],
        0,
    )
    rec.check(
        "digest mismatch => inconsistent",
        hash_result.status == RecoveryStatus.INCONSISTENT,
        str(hash_result.status),
    )
    rec.check(
        "digest reason is digest_mismatch",
        hash_result.reason == InconsistencyReason.DIGEST_MISMATCH,
    )
    rec.check("digest failure leaks no secret", hash_result.secret is None)
    return rec


# ---------------------------------------------------------------------------
# C04：证据不足 / 不一致 / 最大声明规模（31 份、4096 字节）
# ---------------------------------------------------------------------------


def case_c04() -> CheckRecorder:
    seed = 2026111804
    rng = LcgRng(seed)
    t, n, e, length = 5, MAX_SHARES, 1, MAX_BYTES
    secret = make_secret(rng, length)
    raw = make_shares(secret, t, n, rng)
    desc = make_descriptor(secret, t, n)
    rec = CheckRecorder(
        "C04",
        seed,
        f"MAX n={MAX_SHARES}, bytes={MAX_BYTES}, t={t}, e={e}; 规模与失败分类",
    )

    rec.check("max share count is 31", MAX_SHARES == 31)
    rec.check("max byte length is 4096", MAX_BYTES == 4096)
    rec.check("descriptor accepts max scale", len(desc.share_xs) == n and desc.original_length == length)

    # 最大规模下制造一个共同错误份额，计时完成完整纠错恢复。
    corrupt(raw, [9], rng)
    shares = [Share("batch-local", i, raw[i]) for i in range(n)]
    started = time.monotonic()
    big = recover(desc, shares, e)
    elapsed = time.monotonic() - started
    rec.check("max-scale reconstruction OK", big.status == RecoveryStatus.OK, str(big.status))
    rec.check("max-scale bytes equal original", big.secret == secret)
    rec.check("max-scale error position is [9]", big.error_positions == [9])
    rec.note(f"max-scale wall time seconds={elapsed:.2f}")

    # 证据不足：门限 t+2e=7，只提供 6 份。
    few = [Share("batch-local", i, list(raw[i])) for i in range(6)]
    insufficient = recover(desc, few, e)
    rec.check(
        "6 < t+2e=7 => insufficient evidence",
        insufficient.status == RecoveryStatus.INSUFFICIENT_EVIDENCE,
        str(insufficient.status),
    )
    rec.check("insufficient leaks no secret", insufficient.secret is None)
    rec.check("insufficient diagnostics is a list", isinstance(insufficient.diagnostics, list))

    # 不一致：e=1 但制造 2 个共同错误份额（超出声明能力）。
    raw2 = make_shares(secret, t, n, LcgRng(seed ^ 0x3333))
    corrupt(raw2, [3, 17], LcgRng(seed ^ 0x4444))
    over_shares = [Share("batch-local", i, raw2[i]) for i in range(n)]
    over = recover(desc, over_shares, 1)
    rec.check(
        "2 bad shares with e=1 => inconsistent",
        over.status == RecoveryStatus.INCONSISTENT,
        str(over.status),
    )
    rec.check("over-capacity leaks no secret", over.secret is None)
    rec.check(
        "over-capacity reason classified",
        over.reason
        in (InconsistencyReason.DECODE_FAILURE, InconsistencyReason.TOO_MANY_ERRORS),
        str(over.reason),
    )
    rec.check(
        "diagnostics list share numbers only",
        all(isinstance(x, int) and 0 <= x < n for x in over.diagnostics),
        str(over.diagnostics),
    )

    # 批次混入在最大规模下同样不得被插值忽略。
    mixed = list(over_shares)
    mixed[20] = Share("other-batch", 20, list(raw2[20]))
    mix = recover(desc, mixed, 1)
    rec.check(
        "batch mixing at max scale => inconsistent",
        mix.status == RecoveryStatus.INCONSISTENT,
        str(mix.status),
    )
    rec.check("mix reason is batch_mismatch", mix.reason == InconsistencyReason.BATCH_MISMATCH)
    rec.check("mix diagnostics only share number", mix.diagnostics == [20])
    rec.check("mix leaks no secret", mix.secret is None)

    # 描述规模上限之外必须拒绝（描述构造期 ValueError）。
    try:
        BatchDescriptor(
            batch_id="batch-local",
            prime=FIELD_PRIME,
            threshold=t,
            share_xs=list(range(1, MAX_SHARES + 2)),
            original_length=length,
            sha256=desc.sha256,
        )
        oversized = False
    except ValueError:
        oversized = True
    rec.check("32 shares rejected by descriptor", oversized)
    return rec


CASES: Dict[str, Callable[[], CheckRecorder]] = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="secret_recovery 本地核验")
    parser.add_argument("--list", action="store_true", help="列出可用用例")
    parser.add_argument("--case", choices=sorted(CASES) + ["all"], help="选择用例")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({"cases": sorted(CASES)}, ensure_ascii=False, indent=2))
        return 0
    if not args.case:
        parser.error("must specify --list or --case")

    selected = sorted(CASES) if args.case == "all" else [args.case]
    records = []
    exit_code = 0
    for case_id in selected:
        recorder = CASES[case_id]()
        record = recorder.record()
        records.append(record)
        total = record["assertions"]["total"]
        if total == 0 or record["assertions"]["skipped"] == total:
            exit_code = 3  # 零断言或整组跳过。
        elif record["assertions"]["failed"]:
            exit_code = 1
    print(json.dumps(records if args.case == "all" else records[0], ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
