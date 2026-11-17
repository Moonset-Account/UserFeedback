# 终端检测：进程链批量改写文档事件分析

识别短时间内批量把用户文档改写为高熵未知内容的进程派生链，同时避免把
“新建压缩归档且保留源文档”的正常行为误算为文档改写。

本实现只依赖 Python 3.11 标准库，输入为可信采集端给出的事件流，输出为
可 JSON 序列化的分析快照（暂定/确认发现、最小支持事件集合、未知清单、
身份冲突清单、排除清单与统计量）。

## 目录结构

```
doc_rewrite/
  __init__.py    包入口（重导出公开 API）
  events.py      事件数据模型 Event / EntropyRange / EventType 与 JSON 反序列化
  policy.py      策略 Policy（窗口长度、文档/目录阈值、类别集合等）
  analyzer.py    RewriteAnalyzer：乱序重放、风险判定、窗口发现与冲突检测
samples/
  C01_identity_chain.json          重命名/硬链接身份追踪与派生链去重
  C02_qualification_archive.json   关闭确认才计风险 + 新建归档排除
  C03_watermark_out_of_order.json  乱序、水位线、暂定/确认、双阈值、迟到事件
  C04_unknown_conflict.json        缺写前证据 vs 身份冲突 + 最小支持集合
tools/
  local_verify.py                  本地核验入口（真实断言，非演示打印）
```

## 判定语义

1. **文件身份只认 `file_id`**。重命名（rename）与硬链接（hardlink）只更新
   路径别名；同一路径被两个不同 `file_id` 占用即记身份冲突，**绝不按路径
   猜测归并**。冲突事件保留全部 claim（含 seq/ts/类型/进程）供审计。
2. **进程派生链**。spawn 事件维护 `pid -> ppid`，判定时沿父链回溯到链根
   （带环保护）；同一链根对同一 `file_id` 的多次登记类写入合并为一份
   受影响文档记录，`modification_count` 如实记录写入次数。进程世代
   `gen` 随受影响记录输出（`generations` 字段）。
3. **风险成立必须同时满足**：
   - 存在 `pre_category` 属于已登记文档类别（document/office/pdf 等）的写入；
   - 关闭事件确认 `post_category = unknown` 且熵区间**下界**达到
     `high_entropy_threshold`；只写不关不计数，写入阶段不提前计数；
   - 熵区间缺下界等无法证实高熵的情况，不静默丢弃，进入
     `unknown_files`，原因 `entropy_inconclusive`。
4. **新建归档排除**。`new_file=true` 的文件关闭为归档类别（或路径命中
   归档后缀），且写入事件引用的源文档 `file_id` 仍在场（源被保留），
   进入 `excluded`，原因 `new_archive_source_retained`；源文档本身不被
   计为改写。源不在场时不套用该排除。
5. **缺少写前证据单列**。既有文件被改成高熵未知、但没有任何登记类写前
   证据的，不进风险计数，进入 `unknown_files`，原因
   `missing_pre_evidence`；新建文件缺证据则进 `excluded`，原因
   `new_file_no_registered_pre`。未知清单与身份冲突是两张独立表。
6. **窗口发现**。在 `window_seconds` 窗口内，不同 `file_id` 数与独立
   目录数同时达到阈值才产生发现，发现之间不重叠。发现携带
   `support_event_seqs` 最小支持集合：每个 (链, 文件) 取**最早一条**
   登记类写入加关闭确认事件——去掉其中任一事件该文件的改写即无法
   证实；后续写入只增加 `modification_count`，不进入最小集合。
   rename/hardlink 属身份轨迹，在记录的 `identity_event_seqs` 单列。
7. **暂定/确认**。发现的确认就绪时刻
   `confirmation_ready_ts = 窗口内最晚关闭时间 + window_seconds`；
   水位线未越过该时刻前一律 `tentative`，越过后 `confirmed`。水位线
   单调不回退，回退尝试计入 `watermark_regressions`。
8. **乱序与迟到**。快照每次从送达日志按 `(ts, seq)` 重放重建，乱序到达
   不影响最终事实；送达瞬间水位线已越过其时间的事件计入
   `late_events`，仍参与分析但不产生重复受影响记录。

## 公开 API

```python
from doc_rewrite import Event, Policy, RewriteAnalyzer
from doc_rewrite.events import event_from_dict, load_scenario

policy = Policy.from_dict({
    "window_seconds": 60.0,
    "min_documents": 3,
    "min_distinct_dirs": 2,
    "high_entropy_threshold": 7.5,
    "registered_categories": ["document", "office", "pdf"],
    "unknown_category": "unknown",
    "archive_categories": ["archive"],
    # dir_depth=0 使用完整父目录路径；k>0 只保留最后 k 段做粗粒度聚合
    "dir_depth": 0,
})

analyzer = RewriteAnalyzer(policy)
for raw in stream:                       # 允许乱序
    analyzer.ingest(event_from_dict(raw))
analyzer.advance_watermark(120.0)        # 也可由流内 watermark 事件驱动
snapshot = analyzer.snapshot()           # dict，可 json.dumps
```

### 快照字段

| 字段 | 含义 |
| --- | --- |
| `findings[].status` | `tentative` / `confirmed` |
| `findings[].support_event_seqs` | 最小支持事件 seq 集合，附 `support_size` |
| `findings[].confirmation_ready_ts` | 暂定升格为确认所需的水位线时刻 |
| `affected_records[]` | 每条“派生链 × 文件”的受影响记录（含链根、世代、目录、修改次数、支持集合、身份轨迹 seq） |
| `unknown_files[]` | `missing_pre_evidence` 或 `entropy_inconclusive`，附现场 pid、类别、熵区间 |
| `identity_conflicts[]` | 同路径多 `file_id`（或 spawn 父声明冲突）的全部 claim |
| `excluded[]` | `new_archive_source_retained` / `new_file_no_registered_pre` |
| `stats` | 事件数、受影响文档/记录数、发现计数、迟到事件数、水位线回退数等 |

事件类型：`open / write / close / rename / hardlink / unlink / spawn /
watermark`。write/close 携带 `pre_category`、`post_category` 与
`entropy = {low, high}`（端点可为 null）；归档写入用 `new_file=true` 与
`archived_source=<源 file_id>` 表达。

## 本地核验命令

使用工作区自带虚拟环境（标准库即可，无需联网安装）：

```bash
.venv/bin/python -I tools/local_verify.py --list          # 列出用例
.venv/bin/python -I tools/local_verify.py --case C01      # 单用例
.venv/bin/python -I tools/local_verify.py                 # 全部用例
echo "exit=$?"                                             # 0 才为通过
```

每组输出一条 JSON 记录：`case`、实际断言数 `assertions`、
`passed/failed/skipped`、样本/种子路径、失败断言详情（若有）、
期望/观察摘要。任一断言失败、入口缺失、异常、零断言或整组跳过均以
**非零码退出**；框架不提供“跳过”能力。

当前断言规模：C01=35、C02=21、C03=33、C04=25，共 114 条真实断言，
覆盖最小集合“剔除任一事件即削弱”的最小性证明、阈值反例、水位线回退
保护、迟到补到达不改变发现等。

## 可重放样本

四个 `samples/*.json` 均为自包含合成数据（无凭据、无真实用户内容），
可直接重放：

```python
from doc_rewrite import Policy, RewriteAnalyzer
from doc_rewrite.events import load_scenario

policy_dict, events = load_scenario("samples/C04_unknown_conflict.json")
analyzer = RewriteAnalyzer(Policy.from_dict(policy_dict))
for event in events:
    analyzer.ingest(event)
print(analyzer.snapshot())
```

C03 的 `checkpoints` / `late_checkpoint` / `threshold_variants` 字段是
核验驱动专用的编排元数据，分析器本身只读 `policy` 与 `events`。

## 未覆盖边界（审计须知）

- **采集端可信度不在本模块范围内**：`file_id`、类别、熵区间均被视为
  可信输入；若采集端被绕过或伪造，分析器无法自证。审计仍应结合真实
  采集日志与端点取证，不能仅凭本模块输出定性。
- **熵只做区间下界阈值比较**：不自行计算内容熵，也不校验区间数值的
  物理合理性；区间缺失/不完整按保守未知处理（`entropy_inconclusive`）。
- **目录身份基于路径字符串**：符号链接、路径规范化（`.`/`..`、大小写
  不敏感文件系统）不做展开；`dir_depth` 粗粒度聚合可能把不同目录合
  并，默认 `0`（完整父路径）最保守。
- **窗口模型为固定跨度非重叠窗口**：没有实现跨窗口关联、周期性行为
  画像或速率基线；窗口边界附近的爆发可能被切到两个窗口。
- **unlink 仅参与路径 claim**：不模拟文件系统空间回收，也不处理
  file_id 复用（采集端应保证标识唯一）。
- **spawn 缺失时进程自成链根**：无法把无派生关系的多个 pid 归因到同一
  攻击者；spawn 父声明冲突只登记不强行改写父子关系。
- **性能未做专项优化**：快照为全量重放，事件量级按终端单机场景设计，
  超大流需要引入增量状态或分层聚合。
- 核验包装器是**运行入口而非合格证明**：它只能证明合成样本上的断言
  成立；真实环境的结论需要审计人员阅读代码与真实日志，对不能证实的
  内容保留未知。
