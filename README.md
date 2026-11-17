# 终端审计：子进程句柄继承离线分析

离线还原一次子进程启动（launch）实际继承了哪些敏感对象句柄，并对照子进程允许
的对象类别/权限指出多出的能力。分析器只消费事件流与可信采集端快照，不连接任何
真实系统或云服务；所有样本均为本地合成数据。

## 1. 目录结构

```
handleaudit/analyzer.py     # 核心重放与审计分析（无第三方依赖，标准库实现）
handleaudit/__init__.py     # 公共 API 导出
samples/c01_*.json          # 关闭后复用 + 进程世代交错
samples/c02_*.json          # 启动窗口内可继承标志翻转
samples/c03_*.json          # 默认继承 / 显式列表 / 跨进程复制权限
samples/c04_*.json          # 缺快照 + 事件缺口，保留待核实
tools/local_verify.py       # 本地核验入口（--list / --case）
```

运行时要求：Python 3.11（工作区 `.venv` 已就绪），不安装任何额外包。

## 2. 命令

```bash
# 列出可重放用例
python -I tools/local_verify.py --list

# 运行单个用例（C01..C04），输出一个 JSON 对象
python -I tools/local_verify.py --case C01

# 运行全部用例，输出 JSON 数组；任一断言失败则退出码为 1
python -I tools/local_verify.py --case all
```

`-I` 保证隔离模式：忽略当前目录与 `PYTHONPATH` 中的非标准模块，避免被植入的
同名模块（如 `json.py`）劫持。核验器自身以绝对路径把仓库根加入 `sys.path`。

每个用例的 JSON 记录包含：

| 字段 | 含义 |
| --- | --- |
| `case` / `title` / `sample` / `seed` | 用例标识、样本路径与合成种子 |
| `assertion_count` / `passed` / `failed` / `skipped` | 用例级真实断言计数（跳过恒为 0） |
| `determinism_check` | 同一输入连续两次分析输出是否完全一致 |
| `assertions[]` | 每条断言的名称、期望值、观察值、失败细节 |
| `observed_summary` | 分析器实际报告的继承句柄/首个违规摘要 |
| `error` | 分析器或入口异常（存在即判定失败） |

失败语义：任一断言失败、入口缺失或抛异常、用例级零断言，进程均以非零码退出；
不会只打印成功文字。

## 3. 输入数据模型（samples 中的实际 schema）

`events[]`，按 `seq` 严格有序（分析器也会先排序，允许乱序输入）：

| `type` | 关键字段 |
| --- | --- |
| `process_generation` | `pid`, `generation`（pid 复用靠世代区分） |
| `handle_open` | `pid`, `process_generation`, `handle_value`, `object_id`, `object_category`, `granted_access`, `inheritable` |
| `handle_close` | `pid`, `process_generation`, `handle_value` |
| `handle_flag_update` | 同上 + `inheritable`（可继承标志更新） |
| `handle_duplicate` | 源 `(pid, generation, handle_value)`、目标三元组、**事件携带的 `granted_access`** |
| `launch_begin` / `launch_commit` | 父/子 `pid+generation`、`inheritance_mode`（`default`/`explicit_list`）、`explicit_handles`、`snapshot_seq` |

`snapshots[]`：可信采集端按 `snapshot_seq` 记录的子进程句柄清单（对象、类别、
实际授予掩码）。快照序号占用事件流序号，缺口启发式不会把它误判为缺失事件。

`launches[]`：把 begin/commit 序号、继承模式、声称的快照序号配对，是分析的单位。

`policy`：`per_child[pid] = {allowed_categories, allowed_rights}`，未命中子进程时
使用 `default`。类别不在白名单，或掩码解码出的权限不在白名单，均构成违规。

### 句柄身份与权限规则

1. 句柄数值可复用。身份键为
   `(pid, process_generation, open_generation)`；每次 `handle_open` 对该
   `(进程, 数值)` 开启新的 open generation，关闭后重开得到不同实例，报告中
   `source_instance` 形如 `h_100_1_100_o2`（`o2` 即第二次打开）。
2. `handle_duplicate` 在目标进程生成**新实例**，其访问权取事件里的
   `granted_access`，绝不沿用源句柄掩码；来源链节点同时保留
   `source_access` 与新授予值，可直接审计降权/提权。
3. 来源链 `source_chain[]` 按时间顺序记录 `duplicate` 与
   `inheritance:<mode>` 节点，给出完整来源链。
4. 访问掩码通过 `decode_access_mask()` 解码（`READ=0x1 WRITE=0x2 EXECUTE=0x4
   DELETE=0x8 DUPLICATE=0x10 SET_INFORMATION=0x20 READ_CONTROL=0x40
   GENERIC_ALL=0x8000`）；未知位保留为 `UNKNOWN_0x..`，不会被静默丢弃。

## 4. 证据等级（不猜测缺失证据）

每条继承记录的 `status`：

- `confirmed`：有可信快照条目（`evidence=snapshot` /
  `snapshot+duplicate_chain`），或无快照时存在直接指向该子进程世代的
  `handle_duplicate` 事件佐证（`evidence=duplicate_event`）。
- `not_inherited`：快照存在且窗口无缺口，但该父句柄未被快照收录
  （含默认模式下的非可继承句柄，`evidence=non_inheritable_handle`）。
- `unverified`：两种情况——
  - 无快照且无复制事件佐证，仅为模式/标志重放预测
    （`replay_prediction_no_snapshot`）；
  - 启动窗口内有序列缺口，快照是唯一完整权威，无法排除的预测
    （`replay_prediction_gapped_window`）。注意：模式本身在快照时刻即排除的
    句柄（如默认模式下非可继承）即使有缺口仍判 `not_inherited`，缺口不会把它
    “提升”为待核实继承。

报告顶层还给出：

- `sequence_gaps[]`：缺失序号及所属启动窗口；
- `launches_missing_snapshot[]`：声称了快照序号但采集端没有该快照的启动；
- `anomalies[]`：无法解析的句柄、未知事件类型、快照与重放对象不一致等；
- `first_noncompliant_inheritance`：按 commit 序号与句柄值定位的**第一个已确认
  违规**；`unverified` 条目不参与违规计数，缺失证据绝不被猜成结论。

## 5. 公共 API

```python
from handleaudit import analyze_dataset, analyze_events, decode_access_mask

# 直接喂样本文件结构
report = analyze_dataset(dataset)

# 或分别传入三个流；events 允许乱序
report = analyze_events(events, snapshots, launches, policy)

decode_access_mask(0x3)          # ['READ', 'WRITE']
decode_access_mask(0x100)        # ['UNKNOWN_0x100']
```

报告为纯 JSON 可序列化字典（句柄值与掩码均以十六进制字符串呈现），无副作用、
确定性可重放（核验器对每个用例连续分析两次并比对完整 JSON）。

## 6. 四个可重放样本与断言要点

| 用例 | 交错场景 | 关键断言（真实检查，非文字说明） |
| --- | --- | --- |
| C01 | 父进程关闭 0x100 后以同值打开另一对象；pid 200 世代 1 退出、世代 2 复用 pid | 0x100 解析到第二次打开的 `obj-file-B`（`_o2`）而非首个注册表对象；世代 1/2 不串身份；token 的额外 WRITE 被定位为首个违规 |
| C02 | begin 与 commit 之间三次标志翻转 | 以快照序号时刻判定：begin 后才变可继承的句柄已确认继承；快照前变非可继承的句柄为 `not_inherited`；窗口完整、零违规 |
| C03 | 默认继承、显式列表、跨进程复制 | 非可继承互斥量被快照拒绝；显式列表覆盖非可继承标志；复制句柄 0x200 保留事件授予的 READ（源为 READ\|WRITE），链中同时记录两个掩码；首个违规定位到 L3A |
| C04 | L4A 缺快照；L4B 窗口内缺 seq 14 | 缺快照启动只有复制事件佐证的句柄被确认，其余 `unverified`；缺口窗口下注册表句柄保持 `unverified` 且不计违规；首个违规只来自 L4A 的已确认证据 |

## 7. 核验包装器的定位

`tools/local_verify.py` 是**运行入口**，不是独立合格证明。它调用本次实现的
`handleaudit.analyze_dataset` 并对真实报告做硬断言。正式审计仍应阅读
`handleaudit/analyzer.py` 的重放逻辑、核对采集端原始日志，并对报告中所有
`unverified` / `anomalies` 条目保留未知结论，补齐证据后再定性。

## 8. 未覆盖的边界（如实声明）

- 仅建模单主机、纯 JSON 事件流；未接入真实 ETW/Sysmon/auditd 采集，也未做
  原始日志格式解析与采集端时钟漂移校正。
- 假设 `seq` 在可信采集端单调；多采集端合流、序号回绕或并发部分序未建模。
- 策略只覆盖“对象类别 + 已命名权限位”的白名单；未实现对象路径层级规则
  （如目录前缀继承）、SID/所有者约束、完整性级别与 AppContainer 隔离。
- 未模拟句柄表的部分失败（如复制被拒绝但事件仍声称成功之外的操作系统行为）、
  嵌套模拟（impersonation）与作业对象（job object）限制。
- 缺口检测基于序号连续性；若采集端只对部分事件类型编号，需要先在上游规范化，
  本模块不猜测缺口内可能发生的标志翻转或开/关事件。
- 样本中的对象名、令牌、保管库均为合成占位（`synthetic-*`），不含真实凭据；
  核验输出同样不会转存样本原文，只输出句柄值、对象 id 与期望/观察摘要。
