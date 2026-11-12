#!/bin/bash
# 构建并运行 fixcheck 的黄金输出测试
set -u
cd "$(dirname "$0")/.."

BIN=build/fixcheck
mkdir -p build
c++ -std=c++17 -Wall -Wextra -o "$BIN" src/fixcheck.cpp || { echo "BUILD FAIL"; exit 1; }

"$BIN" tests/data/fix_manifest.txt \
       tests/data/compile_units.txt \
       tests/data/archive_members.txt \
       tests/data/link_symbols.txt > build/actual.txt
rc=$?

# 数据中存在非 OK 结论，期望退出码为 1
if [ "$rc" -ne 1 ]; then
    echo "FAIL: 期望退出码 1，实际 $rc"
    exit 1
fi

if diff -u tests/expected.txt build/actual.txt; then
    echo "PASS: 输出与期望一致"
else
    echo "FAIL: 输出与期望不一致"
    exit 1
fi
