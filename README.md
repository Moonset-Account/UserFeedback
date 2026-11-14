# 防火墙规则变更评审工具（SEC-20261114-057）

比较**现行**与**候选**两套有序 JSON 防火墙规则，给出：

1. **动作发生翻转的每个子范围**（允许→拒绝、拒绝→允许），以及翻转前后各自命中的规则；
2. **动作不变但命中规则变化的子范围**（信息项，规则重排时常见）；
3. **候选规则仍未满足业务要求的范围**——业务流量清单中期望动作与候选规则实际动作不一致的精确子段。

匹配维度：来源 IPv4 网段、目标 IPv4 网段、协议（`tcp`/`udp`/`any`）、目标端口范围。
语义：**第一条命中的规则生效；未命中任何规则的流量默认拒绝**。

仅使用 Python 3 标准库，无第三方依赖。

## 运行环境

- Python 3（开发验证使用 `/usr/local/bin/python3`，3.11.6）
- 不需要 pip / venv / 网络

## 快速开始

```bash
# 跑内置“规则重排”样例（细规则从宽泛规则之后移到之前）
/usr/local/bin/python3 app.py sample

# 比较两套规则
/usr/local/bin/python3 app.py diff \
    --current samples/reorder_current.json \
    --candidate samples/reorder_candidate.json

# 检查业务流量清单是否被候选规则满足
/usr/local/bin/python3 app.py check \
    --candidate samples/reorder_candidate.json \
    --business samples/reorder_business.json

# 一次出完整评审报告（diff + 业务检查）
/usr/local/bin/python3 app.py review \
    --current samples/reorder_current.json \
    --candidate samples/reorder_candidate.json \
    --business samples/reorder_business.json \
    --format json
```

所有子命令支持 `--format text|json`（默认 `text`）。

退出码：

| 命令 | 0 | 1 | 2 |
|---|---|---|---|
| `diff` | 报告已生成（有无变化都返回 0） | — | 输入/校验错误 |
| `check` / `review` / `sample` | 业务要求全部满足 | 存在未满足的业务范围 | 输入/校验错误 |

## 输入格式

### 规则集（`--current` / `--candidate`）

JSON 数组，**顺序即优先级**，先命中先生效：

```json
[
  {
    "id": "allow-ops-to-db",
    "action": "allow",
    "src": "10.20.7.0/24",
    "dst": "10.30.8.0/24",
    "proto": "tcp",
    "port": "5432"
  }
]
```

- `id`：可选，非空字符串，缺省自动命名为 `rule-N`；重复 id 报错。
- `action`：必填，`allow` 或 `deny`。
- `src` / `dst`：IPv4 CIDR，缺省 `0.0.0.0/0`。仅支持 IPv4。
- `proto`：`tcp` / `udp` / `any`，缺省 `any`。
- `port`：单端口（`"443"` 或 `443`）、范围（`"8000-9000"`）或 `"any"`（缺省，等价 `1-65535`）。合法端口 1–65535，范围不得倒置。

### 业务流量清单（`--business`）

同样的四个范围字段，外加期望动作：

```json
[
  {
    "id": "biz-partner-to-db",
    "src": "172.16.9.0/24",
    "dst": "10.30.8.0/24",
    "proto": "tcp",
    "port": "5432",
    "expect": "allow"
  }
]
```

- `expect`：必填，`allow` 或 `deny`。
- 一条业务可以覆盖整个大网段；工具会把其中**只有一部分**不满足的情况精确拆出子范围。

## 输出语义

`review` 的 JSON 报告含三个键（`diff` 只含前两个，`check` 只含第三个）：

- `changes[]`：动作翻转的子范围。每条含 `src`/`dst`/`proto`/`port` 以及
  `before`/`after` 两个 `{action, rule}`；`rule: null` 表示命中默认拒绝。
  非 CIDR 对齐的区间会以 CIDR 列表形式给出（如 `["10.20.0.0/22","10.20.4.0/23",…]`）。
- `rule_only_changes[]`：动作不变但命中规则变化的子范围（信息项）。
- `unsatisfied[]`：候选规则未满足业务要求的子范围，含
  `business`（业务 id）、`expected`、`actual`、`matched_rule`
  （`null` 表示默认拒绝）。

子范围在四维匹配空间（源网段 × 目标网段 × 协议 × 端口）上互不重叠；
相邻且判定相同的片段会被合并，输出按源地址排序，结果确定可复现。

## 内置样例：规则重排

`samples/` 演示题目要求的场景——原先被宽泛规则覆盖的细规则移到前面：

- 现行 `reorder_current.json`：`allow-all-internal`（10/8→10/8 全允许）在最前，
  其后的 `deny-office-to-db`、`allow-ops-to-db` 永远不命中。
- 候选 `reorder_candidate.json`：两条细规则移到宽泛规则之前。

评审结果（`app.py sample` 实际输出）：

- `10.20.0.0/16` 去掉运维段 `10.20.7.0/24` 后的两个源子段，到
  `10.30.8.0/24` 的 tcp/5432 由 **allow → deny**
  （前：命中 `allow-all-internal`；后：命中 `deny-office-to-db`）；
- `10.20.7.0/24` 动作不变（allow），但命中规则从 `allow-all-internal`
  变为 `allow-ops-to-db`（rule-only 变化）；
- 业务 `biz-office-to-db`（期望整个 `10.20.0.0/16` 被拒）只有运维例外段
  `10.20.7.0/24` 不满足——展示“一条业务只有一部分受影响”；
- 业务 `biz-partner-to-db`（`172.16.9.0/24` 期望允许）整段仍被默认拒绝。

## 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

核验入口会：

1. 用 `unittest.defaultTestLoader.discover("tests")` 发现测试并要求 ≥1 条；
2. 实际运行整个套件，要求实际执行（未跳过）≥1 条且无失败/错误；
3. 通过 CLI 真实执行内置样例，逐条断言样例/边界预期（12 条断言，
   每条打印实际值与期望值）。

任一步失败以非零退出。也可以单独跑：

```bash
/usr/local/bin/python3 -m unittest discover -s tests -v
```

## 需求到测试/样例的映射

| 需求点 | 覆盖位置 |
|---|---|
| 有序规则首命中、未命中默认拒绝 | `tests/test_fwmodel.py::FirstMatchTest` |
| 允许→拒绝 / 拒绝→允许子范围及前后命中规则 | `tests/test_fwmodel.py::DiffBasicTest`、`tests/test_sample.py::ReorderSampleDiffTest` |
| 新增/删除规则对比默认拒绝的变化 | `DiffBasicTest::test_new_allow_rule_shows_deny_to_allow` 等 |
| 部分重叠只报告交集（不夸大变化面） | `DiffBasicTest::test_partial_overlap_only_reports_intersection` |
| 业务大网段只有一部分受影响 | `tests/test_fwmodel.py::BusinessCheckTest::test_partial_coverage_reports_only_unsatisfied_piece`、`tests/test_sample.py::ReorderSampleBusinessTest::test_office_requirement_partially_unsatisfied` |
| 候选规则仍未满足的业务范围 | `tests/test_sample.py::ReorderSampleBusinessTest`、CLI `check`/`review` |
| 规则重排样例（细规则移到宽泛规则前） | `samples/reorder_*.json`、`tests/test_sample.py`、`app.py sample` |
| 输入校验（CIDR/端口/协议/动作/重复 id） | `tests/test_fwmodel.py::ValidationTest`、`tests/test_cli.py::CliErrorTest` |
| 端口边界 1 / 65535、范围边界命中 | `FirstMatchTest::test_port_range_boundaries`、`ValidationTest::test_port_bounds_accepted` |

## 限制

- 仅支持 IPv4；仅 TCP/UDP（无 ICMP、无源端口、无连接状态）。
- 规则与业务条目数量为中小规模设计（数十至数百条）；区域求交/求差为
  精确算法，规则极多时输出片段数可能按乘积增长。
- 不连接任何真实防火墙/云平台；输入全部来自本地 JSON 文件。
