#!/usr/bin/env python3
"""SEC-VULN-018 本地核验入口(仅标准库, argv 调用, 不使用 shell)。

流程:
  1. 用 /usr/bin/clang++ -std=c++17 从当前源码重新编译 CLI 与原生测试;
  2. 运行原生 C++ 测试(覆盖四个验收用例与三类缺陷模式);
  3. 运行 CLI 样例并校验报告中的关键判定;
  4. 运行 CLI diff 样例并校验补丁前后判定差异。

任何编译失败、零测试或断言失败都会以非零码退出。
"""
import subprocess
import sys
from pathlib import Path

CLANG = "/usr/bin/clang++"
ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
CASES = ROOT / "tests" / "cases"

failures = []


def run(argv, timeout=60):
    print("+ " + " ".join(str(a) for a in argv), flush=True)
    proc = subprocess.run(
        [str(a) for a in argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc


def compile_target(out, sources):
    argv = [CLANG, "-std=c++17", "-Wall", "-Wextra", "-O1", "-o", out] + [
        str(ROOT / s) for s in sources
    ]
    proc = run(argv)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        failures.append(f"编译失败: {out}")
        return False
    return True


def expect(name, text, needle):
    if needle in text:
        print(f"PASS {name}")
        return True
    print(f"FAIL {name} -- 未找到: {needle!r}")
    failures.append(f"{name}: 未找到 {needle!r}")
    return False


def main():
    BUILD.mkdir(exist_ok=True)
    core = ["src/ir.cpp", "src/analyze.cpp", "src/report.cpp"]
    ok = compile_target(BUILD / "secvuln018_analyzer", core + ["src/main.cpp"])
    ok = compile_target(BUILD / "test_analyzer", core + ["tests/test_main.cpp"]) and ok
    if not ok:
        print("== 本地核验失败(编译) ==")
        return 1

    # 1) 原生测试
    proc = run([BUILD / "test_analyzer", CASES])
    print(proc.stdout, end="")
    if proc.returncode != 0 or "失败 0" not in proc.stdout:
        failures.append("原生测试失败")
    if "PASS " not in proc.stdout:
        failures.append("原生测试数量为零")

    # 2) CLI 样例核验
    cli_cases = [
        ("case1_both_branches_safe.ir", ["安全: 4", "待确认: 0"]),
        ("case2_one_branch_unprotected.ir", ["待确认: 2", "首个无法保证长度有效的指令"]),
        ("case3_callsite_independent.ir", ["安全: 2", "待确认: 2", "调用上下文"]),
        ("case4_missing_summary.ir", ["缺少函数摘要", "待确认"]),
        ("case5_overflow_check_after_arith.ir", ["OVERFLOW", "运算后才检查范围"]),
        ("case6_narrow_before_check.ir", ["NARROW", "截断"]),
        ("case7_loop_widening.ir", ["widening", "待确认"]),
        ("case8_recursion.ir", ["递归", "待确认"]),
        ("case9_read_violation.ir", ["违规"]),
        ("case11_summary_ok.ir", ["待确认: 0"]),
    ]
    for fname, needles in cli_cases:
        proc = run([BUILD / "secvuln018_analyzer", "analyze", CASES / fname])
        if proc.returncode != 0:
            print(proc.stderr)
            failures.append(f"CLI 运行失败: {fname}")
            continue
        for needle in needles:
            expect(f"cli.{fname}: {needle}", proc.stdout, needle)

    # 3) 补丁前后判定差异
    proc = run([
        BUILD / "secvuln018_analyzer", "diff",
        CASES / "case10_patch_before.ir",
        CASES / "case10_patch_after.ir",
    ])
    if proc.returncode != 0:
        print(proc.stderr)
        failures.append("CLI diff 运行失败")
    else:
        expect("cli.diff.变化", proc.stdout, "待确认 -> 安全")
        expect("cli.diff.汇总", proc.stdout, "汇总:")

    if failures:
        print("== 本地核验失败 ==")
        for f in failures:
            print(" - " + f)
        return 1
    print("== 本地核验全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
