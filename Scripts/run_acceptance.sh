#!/usr/bin/env bash
# 验收脚本：构建诊断器、运行合成输入、对照验收矩阵断言分类结果
set -euo pipefail

cd "$(dirname "$0")/.."

BIN_DIR="$(swift build --show-bin-path)"
BIN="$BIN_DIR/AuthzAudit"
swift build >/dev/null

OUT_DIR="Reports"
mkdir -p "$OUT_DIR"
JSON_OUT="$OUT_DIR/report.json"
TEXT_OUT="$OUT_DIR/report.txt"

"$BIN" Fixtures/sample-input.json --format json -o "$JSON_OUT" >/dev/null
"$BIN" Fixtures/sample-input.json --format text -o "$TEXT_OUT" >/dev/null
"$BIN" Fixtures/edge-cases.json --format json -o "$OUT_DIR/edge-report.json" >/dev/null

# 使用隔离模式解析不可信目录外的报告文件（报告由本工具生成，-I 仍作为默认卫生实践）
python3 -I - "$JSON_OUT" <<'PY'
import json, sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
findings = {(f["user"], f["code"]): f for f in report["findings"]}
counts = report["findingCounts"]
cycles = report["cycles"]

# 验收矩阵：(用户, 期望分类, 关联撤权事件, 证据编号)
matrix = [
    ("U-ALICE", "INDEPENDENT_GRANT", "REV-1", "OBS-1"),   # 两条来源仅组来源被撤，个人授权合理保留
    ("U-BOB",   "TRANSIENT",         "REV-1", "OBS-2"),   # 同步期限内暂态（850+200=1050 > 1000）
    ("U-CAROL", "RESIDUAL",          "REV-5", "OBS-3"),   # 超期残留（700+200=900 <= 1000）
    ("U-DAN",   "EVIDENCE_STALE",    "REV-1", "OBS-4"),   # 快照 800 早于水位 900
    ("U-EVE",   "EVIDENCE_CYCLE",    "REV-3", "OBS-5"),   # G-X <-> G-Y 成环
    ("U-FRANK", "EVIDENCE_MISSING",  "REV-4", "OBS-6"),   # 快照缺失
    ("U-HEIDI", "UNEXPECTED_ALLOW",  None,    "OBS-8"),   # 授权过期且无撤权事件
    ("U-IVAN",  "INDEPENDENT_GRANT", "REV-1", "OBS-9"),   # 两条来源仅组来源被撤（与 Alice 同构的独立授权）
]

failures = []
for user, code, rev, evidence in matrix:
    key = (user, code)
    if key not in findings:
        failures.append(f"缺少 {user} 的 {code} 分类；实际: {[f['code'] for u,f in findings if u==user]}")
        continue
    f = findings[key]
    if f.get("revocationEvent") != rev:
        failures.append(f"{user}/{code}: 撤权事件期望 {rev}，实际 {f.get('revocationEvent')}")
    if f.get("evidenceId") != evidence:
        failures.append(f"{user}/{code}: 证据编号期望 {evidence}，实际 {f.get('evidenceId')}")
    if not f["check"]:
        failures.append(f"{user}/{code}: 缺少应核对的快照/规则指引")

# 对照组：Grace（显式拒绝优先）与 Judy（观察与决策一致、路径上无撤权）不得产生差异
for user in ("U-GRACE", "U-JUDY"):
    if any(u == user for u, _ in findings):
        failures.append(f"{user} 不应有差异项，但产生了 {[c for u,c in findings if u==user]}")

# Grace 的有效决策必须是 deny（R2 显式拒绝覆盖已撤销的组 allow）
grace = next(e for e in report["effectivePermissions"] if e["user"] == "U-GRACE")
if grace["decision"] != "deny" or grace["denyGrants"] != ["GR-GRACE-DENY"]:
    failures.append(f"R2 显式拒绝未生效: {grace}")

# 环检测必须报告 G-X/G-Y 环及成员关系证据
if not cycles or set(cycles[0]["cycleGroups"]) != {"G-X", "G-Y"}:
    failures.append(f"环检测结果异常: {cycles}")
if set(cycles[0]["membershipChain"]) != {"M-8", "M-9"}:
    failures.append(f"环的成员关系证据编号异常: {cycles}")

# 计数自洽
for code, n in counts.items():
    if sum(1 for f in report["findings"] if f["code"] == code) != n:
        failures.append(f"分类计数不自洽: {code}={n}")

# 规则文档优先级固定
prio = [r["ruleId"] for r in report["rules"]]
if prio != ["R1", "R2", "R3", "R4", "R5"]:
    failures.append(f"规则优先级顺序异常: {prio}")

if failures:
    print("验收失败：")
    for x in failures:
        print("  -", x)
    sys.exit(1)

print("验收通过：%d 项差异全部落在期望分类，撤权事件/证据编号/核对指引齐全" % len(report["findings"]))
print("分类计数：", dict(sorted(counts.items())))
PY

# 边界夹具：时间边界、自环、未知快照
python3 -I - "$OUT_DIR/edge-report.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
findings = {(f["user"], f["code"]): f for f in report["findings"]}
expect = [
    ("U-T1",    "TRANSIENT",       "ERV-1"),  # t == 撤权900+期限100 == 1000，边界内
    ("U-T2",    "RESIDUAL",        "ERV-2"),  # 截止 999 < 1000，超期 1 秒
    ("U-LOOP",  "EVIDENCE_CYCLE",  "ERV-3"),  # 自环
    ("U-GHOST", "EVIDENCE_MISSING", None),    # 未登记快照
]
failures = []
for user, code, rev in expect:
    f = findings.get((user, code))
    if f is None:
        failures.append(f"边界夹具缺少 {user}/{code}")
        continue
    if f.get("revocationEvent") != rev:
        failures.append(f"{user}/{code} 撤权事件期望 {rev}，实际 {f.get('revocationEvent')}")
if not any(set(c["cycleGroups"]) == {"G-SELF"} for c in report["cycles"]):
    failures.append("未检测到 G-SELF 自环")
if failures:
    print("边界验收失败：")
    for x in failures: print("  -", x)
    sys.exit(1)
print("边界验收通过：暂态/残留时间边界、自环、未知快照均正确")
PY

echo "报告已生成: $JSON_OUT 与 $TEXT_OUT"
