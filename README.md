# 内存缺陷修复分诊工具（memtriage）

本地运行的 C++17 分诊工具，读取三类合成记录——缺陷影响记录、版本部署数量、缓解措施验证结论——
计算每个缺陷的受影响范围、业务中断等级、缓解覆盖与证据置信度，按确定性规则给出修复顺序，
并能对比两个数据集、解释新增部署或更正缓解证据后修复顺序为何改变，为证据不足的条目生成核实清单。

设计上**零外部依赖**（自带 JSON 解析器），可在仅含 C++17 编译器的环境离线构建。

## 1. 构建与运行

需要支持 C++17 的编译器（clang++ / g++）与 make：

```bash
make            # 构建 build/memtriage，开启 -Wall -Wextra -Wpedantic -Wshadow -Wconversion
make test       # 构建并运行 80 项单元测试
make run-demo   # 用 data/ 下的合成数据生成 build/report.txt 与 build/report.json
make clean
```

### 命令行

```bash
./build/memtriage --input <数据目录> [--baseline <基线目录>] [--json] [--out <文件>]
```

| 参数 | 说明 |
| --- | --- |
| `--input <目录>` | 必需。目录内需含三个输入文件（见下） |
| `--baseline <目录>` | 可选。提供后启用两期对比，解释修复顺序变化 |
| `--json` | 输出机器可读 JSON；默认输出中文文本报告 |
| `--out <文件>` | 写入文件；默认写到标准输出 |

退出码：`0` 成功；`1` 参数错误；`2` 数据或评估失败（错误信息输出到 stderr）。

## 2. 输入数据格式

每个数据目录包含三个 UTF-8 JSON 文件，枚举值非法、引用悬空、ID 重复等情况会被明确拒绝而非静默忽略。

### 2.1 `defect_impacts.json` — 缺陷影响记录（编号已去重）

```json
{
  "defects": [
    {
      "defect_id": "MEM-1002",
      "title": "活跃发行版上的释放后使用，中止关键进程",
      "impact_level": "critical_process_abort",
      "alert_count": 37,
      "evidence_confirmed": true,
      "evidence_source": "crloop-correlator",
      "affected_releases": ["REL-S2", "REL-S3", "REL-S4"]
    }
  ]
}
```

`impact_level` 取值与中断权重 W：

| 枚举 | 含义 | W |
| --- | --- | --- |
| `critical_process_abort` | 关键进程中止 | 4 |
| `business_interruption` | 业务中断 | 3 |
| `service_degradation` | 服务降级 | 2 |
| `minor_degradation` | 轻微降级 | 1 |

### 2.2 `release_deployments.json` — 版本部署数量

```json
{
  "deployments": [
    { "release_id": "REL-S2", "lifecycle": "active",  "deploy_count": 1500 },
    { "release_id": "REL-S4", "lifecycle": "active",  "deploy_count": null },
    { "release_id": "REL-EOL-9", "lifecycle": "retired", "deploy_count": 60 }
  ]
}
```

- `lifecycle`：`active`（活跃）/ `retired`（退役）/ `unknown`（未知）。
- `deploy_count` 为 `null` 表示**部署范围未知**：工具不会把它当作 0，也不猜测数值，
  而是将其排除在受影响部署规模之外、单独计入核实清单并扣减置信度。
- 缺陷影响了但部署表中缺失的发行版，按「生命周期未知 + 部署未知」谨慎处理。

### 2.3 `mitigation_verifications.json` — 缓解措施验证结论

```json
{
  "mitigations": [
    { "mitigation_id": "MIT-2001", "defect_id": "MEM-1002", "release_id": "REL-S2",
      "verification_status": "verified_effective", "evidence_note": "复测 200 次未复现" }
  ]
}
```

`verification_status` 取值：`verified_effective`（已核实有效）、`ineffective`（已核实无效）、
`inconclusive`（结论不确定）、`pending`（待验证）、`disputed`（存在争议）。

**缓解覆盖口径（硬性规则）：只有 `verified_effective` 的发行版计入缓解覆盖。**
其余四种状态以及没有任何缓解记录的发行版，一律不计入覆盖并进入核实清单。
同一缺陷在同一发行版上存在多条记录时，优先采用已核实有效的结论；
同为非有效结论时按 `ineffective > disputed > inconclusive > pending` 的确定性次序展示，
次序仍相同则按缓解编号字典序。

## 3. 评分模型

对每个缺陷，按其受影响发行版逐项评估后汇总：

| 指标 | 定义 |
| --- | --- |
| 暴露部署 E | 活跃与生命周期未知的发行版中，**部署数已知**的部署总量。退役发行版不计入 |
| 覆盖部署 M | 结论为已核实有效、且部署数已知的发行版部署量之和 |
| 未缓解部署 U | `U = E − M` |
| 缓解覆盖率 | `M / E`；`E = 0` 时报告 `N/A`（不做除零） |
| 风险分 R | `R = W × U`，整数语义 |

告警次数**不进入风险分**。这样「告警次数最多但只出现在退役版本」的缺陷风险分为 0，
不会压过「告警很少但会中止活跃发行版关键进程」的缺陷——这正是本工具要纠正的核心误判。

### 证据置信度（0.0 ~ 1.0，起始 1.0，逐项扣分后截断）

| 扣分项 | 分值 |
| --- | --- |
| 每个部署范围未知的在保发行版 | −0.20 |
| 每个生命周期未知的在保发行版 | −0.10 |
| 每个缺少已核实有效缓解的在保发行版 | −0.05 |
| 影响证据未经确认（`evidence_confirmed=false`） | −0.25 |

分档：`≥ 0.80` 高（high）、`≥ 0.50` 中（medium）、其余为低（low）。
中/低档条目强制出现在报告的「证据不足条目核实清单」中，按
`unknown_deployment` / `lifecycle_unknown` / `mitigation_unverified` / `impact_evidence`
四类给出可执行的核实动作。

## 4. 排序规则与同分稳定次序

比较器是一个**确定性全序**，与输入记录的排列顺序无关，重复运行结果完全一致：

1. 风险分 R 降序；
2. 中断权重 W 降序（同分时更严重的中断优先）；
3. 未缓解部署 U 降序；
4. 证据置信度**升序**（其他条件相同时，证据更弱、更需要先核实的缺陷优先）；
5. 告警次数降序（仅作为风险同分后的辅助信号）；
6. 缺陷编号字典序升序（最终兜底，保证次序唯一可复现）。

## 5. 两期对比：顺序为何改变

传入 `--baseline` 后，工具对两期数据分别分诊，再逐缺陷、逐发行版比对并输出：

- **排名反转对**：哪些缺陷的相对次序发生了交换；
- **逐缺陷归因**，覆盖三类变化：
  - 新增/缩减部署、部署范围由未知补全（E、U 的变化）；
  - 缓解证据更正（如 `pending`/`disputed` → `verified_effective`，M 的变化），
    并明确标注「仅 verified_effective 计入覆盖」；
  - 生命周期更正、发行版移入/移出受影响范围、置信度变化；
- 新增缺陷（`is_new`）与移除缺陷（`is_removed`）单独标记。

`data/` 中的合成示例演示了一次完整反转：

- 基线期 MEM-1003（业务中断，无有效缓解）风险分 5700 排第一，MEM-1002（关键进程中止）2800 排第二；
- 当前期 REL-S2/REL-S3 新增部署推高 MEM-1002 的未缓解暴露，同时 MEM-1003 的两条缓解被核实有效，
  其未缓解部署下降；最终 MEM-1002 以 6000 反超 MEM-1003 的 4500，升到第一。
  报告逐行列出「REL-S2 部署 300 → 1500」「缓解证据更正为已核实有效，M: 0 → 2700」等可审计原因。

## 6. 测试覆盖

`tests/tests.cpp` 含 80 个断言，无第三方测试框架，`make test` 直接运行：

- **退役版本的高频告警**：MEM-1001 告警 4820 次但发行版全退役，E=U=R=0，排名垫底且不产生核实项；
- **关键进程的低频异常**：MEM-1002 仅 37 次告警，因 W=4 且在活跃发行版有未缓解暴露，排名第一；
- **只覆盖部分发行版的缓解记录**：pending/disputed/inconclusive/ineffective 均不计入覆盖，
  仅 verified_effective 计入；同一发行版多记录时有效结论优先；
- **未知部署范围**：`deploy_count=null` 与部署表缺失都不按 0 处理，触发核实清单并扣减置信度；
- **同分稳定次序**：构造完全同分的缺陷验证字典序兜底、告警次序键，以及重复运行可复现；
- **基线对比**：验证排名反转检测、逐缺陷归因（新增部署、缓解证据更正）、新增/移除条目；
- **JSON 解析与报告**：解析器对非法输入报错，自生成的 JSON 报告可被回读且结构合法；
- **输入校验**：空列表、非法枚举、重复 ID、悬空缓解引用均被拒绝。

## 7. 目录结构

```
.
├── Makefile                  # 构建、测试、演示目标
├── README.md
├── data/
│   ├── current/              # 当前期合成数据（三类输入文件）
│   └── baseline/             # 基线期合成数据，用于对比演示
├── src/
│   ├── json.{hpp,cpp}        # 零依赖 JSON 解析/转义
│   ├── model.{hpp,cpp}       # 枚举与领域结构
│   ├── loader.{hpp,cpp}      # 输入装载与严格校验
│   ├── engine.{hpp,cpp}      # 范围/覆盖/置信度计算与确定性排序
│   ├── compare.{hpp,cpp}     # 两期对比与变化归因
│   ├── report.{hpp,cpp}      # 中文文本报告与 JSON 报告
│   └── main.cpp              # CLI 入口
└── tests/
    └── tests.cpp             # 80 项单元测试
```
