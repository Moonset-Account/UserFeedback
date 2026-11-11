# 离线漏洞适用性裁决器（SEC-VULN-038）

针对“两个产品发行版本装入同名组件的不同变体、公告更新后平台仍把两个版本
都标为已修复”的问题，本工具只依据三类元数据重新做出独立裁决：

1. **组件清单**（CycloneDX 风格 BOM，按产品发行版本给出快照）；
2. **合成漏洞公告**（受影响 / 不受影响声明，含撤回与修订生命周期）；
3. **适用性记录**（按发行版本签发，绑定清单快照号）。

全程离线，仅使用 Python 3 标准库；不访问网络、不调用任何评分或情报服务。

## 1. 快速开始

```bash
# 完整运行（含公告更新前后变化报告），推荐 -I 隔离模式
python3 -I run_adjudicator.py \
  --boms data/boms.json \
  --advisories data/advisories-current.json \
  --records data/applicability-records.json \
  --baseline-advisories data/advisories-baseline.json \
  --json-out out/report.json \
  --text-out out/report.txt

# 不带基线时只做当前快照裁决，不输出 change_report
python3 -I run_adjudicator.py --boms ... --advisories ... --records ...

# 运行全部 38 个测试
python3 -I run_tests.py -v
```

`-I`（isolated mode）会忽略 `PYTHONPATH` 与当前目录，避免与输入数据同目录下
被植入的同名模块（如 `json.py`）被解释器加载。输出目录不存在时会自动创建；
同样的输入重复运行产生**字节级一致**的报告。

## 2. 组件身份模型

组件由四元组共同识别，缺一不可：

```
(ecosystem 生态, namespace 命名空间, name 名称, version 版本)
```

- 同名但生态不同（如 `npm:acme/widget` 与 `pypi:acme/widget`）是不同组件，
  证据互不串用；
- 同一清单内可以并存同一坐标的多个版本（不同 `bom.ref`），逐条独立裁决；
- 公告与适用性记录用不含版本的三元组坐标 `(ecosystem, namespace, name)`
  匹配组件，再用版本区间 / 精确版本判断适用性。

## 3. 版本排序规则

### 3.1 语法

```
<MAJOR>.<MINOR>.<PATCH>[-<prerelease>][+<build>]
```

- 核心为**三段点分非负整数**，禁止前导零（`1.01.0` 非法，`0.0.0` 合法）；
- 预发布段为点分标识符，标识符由 `[0-9A-Za-z-]` 组成；纯数字标识符同样
  禁止前导零；
- build 元数据保留在原始字符串中但**不参与排序**；
- 不符合语法的版本（两段式 `1.2`、四段式 `1.2.3.4`、日期式 `20261109-r1`、
  通配符 `1.*.0` 等）解析结果为 `None`，裁决一律降级为“待核实”，
  绝不退化为字符串比较。

### 3.2 排序（参照 SemVer 2.0.0 第 11 条）

1. 核心三段按数值依次比较；
2. 同核心下正式版高于任何预发布版：`1.0.0 > 1.0.0-rc.1`；
3. 预发布标识符从左到右比较：数字按数值、字母数字按 ASCII 字典序，
   数字标识符优先级低于字母数字标识符（`1 < rc`），前缀相同时长者为大；
4. 标准链：`alpha < alpha.1 < alpha.beta < beta < beta.2 < beta.11
   < rc.1 < 正式版`。

## 4. 区间、端点与无界区间

区间用 JSON 对象描述，四个字段均可省略：

```json
{"lower": "1.2.0", "lower_inclusive": true,
 "upper": "1.2.7", "upper_inclusive": false}
```

- `lower` / `upper` 省略或为 `null` 表示该侧**无界**：
  下界无界即 `(-inf, …)`，上界无界即 `(…, +inf)`，两端皆无界覆盖一切版本；
- `lower_inclusive` 缺省 `true`（闭端 `>=`），`false` 为开端（`>`）；
- `upper_inclusive` 缺省 `false`（开端 `<`，与 OSV/GHSA 的 fixed 事件惯例
  一致），`true` 为闭端（`<=`）；
- 成员判定是**三态**的：`INSIDE` / `OUTSIDE` / `UNKNOWN`。
  组件版本不可解析、或区间端点本身不可解析时返回 `UNKNOWN`，
  对应处置只能是“待核实”，并保留具体原因（如
  `unparseable_interval_endpoint:STMT-…:[1.0, 2.0)`）；
- 下界高于上界的区间在装载阶段直接报错。

## 5. 四种处置状态

每个 `(发行版本, 组件实例, 漏洞)` 组合输出一条 finding，处置必为四种之一：

| 处置 | 含义 | 典型证据 |
| --- | --- | --- |
| `affected` 受影响 | 有效证据一致判定版本落在受影响区间 | 受影响公告声明 |
| `not_affected` 不受影响 | 证据一致判定版本被显式豁免（精确版本/豁免区间/有效记录） | 豁免声明、有效适用性记录 |
| `fixed` 已修复 | 存在该漏洞的受影响公告，而版本确定位于全部受影响区间之外（越过修复端点） | 受影响公告 + `outside_affected_range` 原因 |
| `needs_verification` 待核实 | 无法定论的一切情形 | 见下 |

`fixed` 与 `not_affected` 的关键区别：前者必然伴随同一漏洞的受影响公告，
只是当前版本已越过修复边界；后者可能是公告范围之前的旧版本豁免或厂商记录。

### 5.1 待核实（保留未知 / 冲突原因）的触发条件

- `version_not_comparable` / `unparseable_component_version:*`：
  组件版本无法解析或与端点无法比较；
- `unparseable_interval_endpoint:*`：公告区间端点不可解析；
- `conflicting_evidence:*`：不同公告（或公告与有效记录）给出互斥的确定性
  结论，例如同一版本同时被一条公告判受影响、另一条判不受影响；
- `contradiction_within_advisory:*` / `contradiction_within_records:*`：
  同一公告或同一组有效记录内部自相矛盾；
- `record_stale:REC-*:expected_snapshot=…:actual_snapshot=…`：
  适用性记录绑定的快照号与当前 BOM 不一致（**过期记录不进入证据列表**，
  只在原因中留痕）；
- `advisory_withdrawn:ADV-*`：相关公告全部处于撤回状态，无有效证据；
- `no_matching_statement:*` / `no_applicable_advisory`：
  公告存在但没有任何声明可覆盖该版本。

### 5.2 证据编号约定

- `ADV-*`：公告；`STMT-*`：公告内声明；`REC-*`：适用性记录。
- 过期记录、撤回公告只出现在 `reasons`，绝不出现在 `evidence`。
- BOM 的 `bom.ref` 作为组件定位信息随 finding 输出，本身不是判定证据。

## 6. 公告更新前后变化报告

提供 `--baseline-advisories` 时，工具对**同一份 BOM** 分别用两套公告裁决，
按 `(release_id, bom_ref, vuln_id)` 对齐后输出差异，每条变化只归因于
与该漏洞相关的公告生命周期事件：

- `advisory_added` / `advisory_removed`：公告出现或消失；
- `advisory_revised:ADV-…:旧时间->新时间`：公告修订（如受影响区间扩大）；
- `advisory_withdrawn` / `advisory_reopened`：撤回与撤回后重新打开；
- `evidence_added` / `evidence_removed`：适用性记录证据集合变化；
- 同时给出四种处置的统计计数前后对比。

本仓库数据演示的主场景：基线公告只覆盖 `libz [1.4.0, 1.5.0)`，
beta 发行版里的 `1.6.0` 判 **已修复**；更新后区间扩大为 `[1.4.0, 2.1.0)`，
`1.6.0` 翻转为 **受影响**——即使平台仍标“已修复”，本工具依据元数据
独立改判，并在变化报告中记录 `advisory_revised` 原因。

## 7. 输入数据格式

```text
data/
├── boms.json                      # BOM 数组：release_id + snapshot_id + components
├── advisories-baseline.json       # 更新前公告数组
├── advisories-current.json        # 更新后公告数组
└── applicability-records.json     # 适用性记录数组
```

公告示例（受影响区间 + 撤回标记）：

```json
{
  "advisory_id": "ADV-2026-1009",
  "vuln_id": "CVE-2026-1009",
  "revised": "2026-11-08T00:00:00Z",
  "withdrawn": false,
  "statements": [
    {"statement_id": "STMT-1009-A2", "ecosystem": "gem", "namespace": "",
     "name": "ghost", "kind": "affected",
     "affected_intervals": [
       {"lower": "4.0.0", "upper": "5.0.0",
        "lower_inclusive": true, "upper_inclusive": false}]}
  ]
}
```

适用性记录示例（快照不匹配即过期）：

```json
{"record_id": "REC-3001", "release_id": "prod-alpha-3.0",
 "bom_snapshot_id": "snap-alpha-2026-09-01",
 "ecosystem": "maven", "namespace": "org.example", "name": "libz",
 "version": "2.0.1", "vuln_id": "CVE-2026-1001",
 "verdict": "not_affected"}
```

## 8. 测试覆盖（tests/）

| 测试文件 | 覆盖的题目要求 |
| --- | --- |
| `test_versioning.py` | 三段式解析、前导零拒绝、预发布排序链、build 不参与排序、不可比较即报错 |
| `test_interval.py` | 开/闭端点、预发布版本落在边界、单侧无界、双侧无界、畸形端点三态 UNKNOWN |
| `test_engine.py` | 同名异生态、同包并存两版本、区间端点、过期不受影响记录、撤回后重新打开、公告互相矛盾、版本无法比较、CLI 端到端、可重跑一致性 |

测试数据中的 11 个漏洞公告（CVE-2026-1001 ~ 1011）分别对应上述场景，
其中 CVE-2026-1007 由两条互相矛盾的公告组成、CVE-2026-1008 的区间端点
故意写成不可解析的 `1.0`。

## 9. 目录结构

```text
adjudicator/
├── versioning.py   # 版本解析与排序
├── interval.py     # 区间与三态成员判定
├── models.py       # 数据模型与处置常量
├── loader.py       # JSON 装载与校验
├── engine.py       # 裁决、冲突检测、快照过期判定、差异报告
├── report.py       # JSON / 文本报告
└── cli.py          # 命令行入口
run_adjudicator.py  # 启动脚本
run_tests.py        # 测试启动脚本
```
