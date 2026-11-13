# SEC-VULN-050 — 漏洞扫描器配对比较器（离线）

判断新版扫描器（v2）相对旧版（v1）的新增命中是否来自真实改进。纯 Python 3
标准库实现，全部输入为本地合成 JSON，不访问任何外部系统。

## 运行

```bash
# 生成报告（JSON，输出到 stdout；加 --out report.json 写入文件）
/usr/local/bin/python3 app.py \
    --cases fixtures/cases.json \
    --annotations fixtures/annotations.json \
    --v1 fixtures/results_v1.json \
    --v2 fixtures/results_v2.json

# 本地核验（unittest 套件 + 样例边界断言，全部通过才退出 0）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

- `cases.json`：`{"cases": [{"case_id": "C1", "difficulty": "..."}, ...]}`
- `annotations.json`：`{"annotations": {case_id: [{"ann_id", "family",
  "location"}, ...]}}`。空列表表示该案例**明确无漏洞**；case_id 不在表中
  表示**标注缺失**。
- `results_vN.json`：`{"version": "vN", "results": {case_id: {"status":
  "ok"|"timeout"|"error", "duration_ms": int|null, "alerts": [{"alert_id",
  "family", "location"}, ...]}}}`

## 匹配规则

1. 告警与标注匹配当且仅当 **案例 + 漏洞族 + 位置** 三者完全相同。
2. 每条标注最多被一条告警消费（按告警顺序取首个匹配），每条告警最多满足
   一条标注 —— 一条标注不会计入多次命中。
3. 同一案例中同族不同位置的标注**不合并**，各自独立判定。
4. 匹配后剩余的告警计为误报（重复告警的第二条及以后因此成为误报）。

## 输出语义

- `cases[]`：案例级配对明细。`evaluated` 案例给出逐标注的 v1/v2 命中判定、
  命中/漏报/误报计数、`new_hits`（v2 命中而 v1 漏报 = 真实改进）与
  `lost_hits`（回归）；`undecidable` 案例给出排除原因
  （`missing_annotation` / `v1_status:*` / `v2_status:*`）。
- `families`：按漏洞族汇总的 v1/v2 命中、漏报、误报及新增/丢失命中数。
- `summary`：仅在**双方均 ok 且标注齐全**的交集上比较
  （`paired_evaluable_cases`）；另单列全量运行覆盖率 `run_coverage`、
  无法判定清单 `undecidable_cases` 与样本耗时 `timing`
  （仅统计 ok 运行的耗时，超时单独计数）。

## 样例手算期望值（fixtures/）

可评估交集：C1、C2、C3、C6（C4 缺标注；C5 为困难案例、v2 超时）。

| 案例 | v1 命中/漏报/误报 | v2 命中/漏报/误报 | 配对结论 |
|------|------------------|------------------|----------|
| C1（sqli 两处独立缺陷 A1/A2） | 1/1/0 | 2/0/0 | new_hits=[A2] |
| C2（明确无漏洞） | 0/0/1 | 0/0/0 | v1 误报 sqli@src/z.py:1 |
| C3（xss A3） | 1/0/0 | 0/1/0 | lost_hits=[A3]（回归） |
| C6（cmdi A5，v1 重复告警） | 1/0/1 | 1/0/0 | 重复告警 → 1 误报 |

- 合计：v1 = 3 命中 / 1 漏报 / 2 误报；v2 = 3 命中 / 1 漏报 / 0 误报。
- 按族：sqli v1={1,1,1} v2={2,0,0}（new=1）；xss v1={1,0,0} v2={0,1,0}
  （lost=1）；cmdi v1={1,0,1} v2={1,0,0}。
- 运行覆盖率：v1 = 6/6 = 1.0；v2 = 5/6 ≈ 0.8333（困难案例 C5 超时被跳过，
  覆盖率下降在报告中可见）。无法判定 2 例：C4（missing_annotation）、
  C5（v2_status:timeout）。
- 耗时：v1 6 次 ok × 100ms → 总 600ms、均值 100.0；v2 5 次 ok × 120ms →
  总 600ms、均值 120.0、超时 1 次。

## 限制

- 仅处理合成离线输入；匹配为 family+location 精确相等，不做模糊/近似归并。
- 标注缺失与非 ok 状态（timeout/error/missing）的案例一律不进入配对比较，
  只在覆盖率和无法判定清单中体现。
- 耗时统计只覆盖 ok 运行；超时运行没有可信耗时，单独计数。
