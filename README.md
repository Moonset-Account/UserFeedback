# 审批权限判定模块（approval_service）

根据登录主体的**部门**与**在岗状态**决定其能否操作敏感资源。目录更新可能迟到、
也可能包含历史更正，因此模块对每条目录修订分别记录**生效时间**
（`effective_at`）与**接收时间**（`received_at`），同时支持当前授权与针对
既往决定的复核。

## 目录结构与实际入口

```
workspace/
├── approval_service/          # 被测模块（纯标准库，Python 3.11）
│   ├── __init__.py            # 对外导出入口
│   ├── model.py               # 不可变领域模型：记录、请求、规则、决定、复核报告
│   ├── catalog.py             # 目录存储 + 某时刻可见视图解析（DirectoryCatalog / resolve_view）
│   └── service.py             # 判定服务：ApprovalService.decide / review
├── tests/
│   └── test_approval.py       # unittest 验收用例（20 个，真实断言）
├── run_all_tests.py           # 测试入口脚本（发现 tests/test_*.py，按结果设置退出码）
└── README.md
```

### 运行入口

在工作区根目录执行（`-I` 隔离模式，避免当前目录/环境变量中的第三方代码影响）：

```bash
# 入口一：专用脚本
python3 -I run_all_tests.py

# 入口二：标准 unittest 发现
python3 -I -m unittest discover -s tests -p "test_*.py" -t .
```

两种入口均已在本地 Python 3.11.6 验证通过（20/20 OK，退出码 0）。

### 编程入口

- `ApprovalService(catalog, freshness_threshold=timedelta(...))`
  - `register_resource(ResourceRule(...))`：注册资源规则（`sensitive`、允许部门集合）。
  - `register_business_scope(BusinessScope(...))`：注册普通请求所属业务的允许范围。
  - `decide(AccessRequest(...)) -> Decision`：当前授权，决定写入只读台账。
  - `review(request_id, reviewed_at) -> ReviewReport`：对既往决定做复核，只追加报告。
- `DirectoryCatalog.add_record(DirectoryRecord(...))`：写入目录修订；记录 id 不可重复、
  不可覆盖。所有时间点必须是带时区的 `datetime`，否则构造即报错。

## 判定规则

### 敏感请求（`ResourceRule.sensitive=True`）

满足全部条件才 `ALLOW`，否则转人工 `MANUAL_REVIEW`，原因写入 `Decision.reasons`：

1. 判定时刻存在已接收（`received_at <= now`）且已生效（`effective_at <= now`）的记录；
2. 可见记录之间不存在无法消解的冲突（见下）；
3. 主体部门在资源允许部门集合内（集合为 `None` 时不限部门）；
4. 主体在岗（`on_duty=True`）；
5. 适用记录未超出新鲜度阈值：`now - effective_at > threshold` 即过期
   （阈值边界为闭区间，年龄恰好等于阈值不算过期）。

### 普通请求（`sensitive=False`）

按所属业务的允许范围（`BusinessScope`）处理，**不转人工**：

- 部门不在业务允许集合内、或业务要求在岗但主体离岗 → `DENY`；
- 无可见/已生效记录、或记录冲突导致部门无法解析 → 安全侧 `DENY`；
- 新鲜度不阻断普通请求，仅在 `reasons` 中留下 `stale_directory_record_advisory` 咨询标记。

### 冲突与取代（supersedes）

- 同一主体同一时刻存在多条已生效、且**未通过显式 `supersedes` 链消解**、部门/在岗
  状态不一致的记录时，判定为冲突：
  - 敏感请求 → `MANUAL_REVIEW`（原因 `conflicting_directory_records`）；
  - 普通请求 → `DENY`。
- `supersedes` 显式声明取代关系；被取代的记录退出有效根集合，状态一致即消解冲突。

## 时间语义与复核规则

- **可见性看接收时间，适用性看生效时间**：迟到的历史更正 `received_at` 晚于原决定，
  因此对原决定不可见；原决定始终基于当时系统实际可见的输入作出。
- 每个 `Decision` 永久保存：适用记录 id、可见记录 id 列表、可见输入快照
  （`visible_inputs`）以及规范化 SHA-256 输入指纹（`input_fingerprint`）。
- 复核采用 **as-of 语义**：
  - 可见性按复核时刻（迟到更正在复核时已可见）；
  - 适用性与新鲜度按**原决定时刻**评估（被复核的是当时的决定）。
- 复核结论（`ReviewOutcome`）：
  - `UPHELD`：当前结论与原决定一致；
  - `OVERTURNED`：历史更正使结论发生翻转（如原 DENY → 现 ALLOW，或原 ALLOW → 现 MANUAL_REVIEW）；
  - `REINFORCED`：原决定为转人工，复核得到明确的 ALLOW/DENY 结论。
- 复核只产生新的 `ReviewReport`，**不修改、不删除**原始 `Decision`；同一请求 id
  重复判定直接报错，保证“系统当时可见输入所作的实际决定”可审计、不可篡改。

## 验证范围（tests/test_approval.py，20 个用例）

| 分组 | 覆盖内容 |
| --- | --- |
| `SensitiveDecisionTests` | 可接受结果放行；离岗/部门越权转人工；新鲜度阈值闭区间边界（恰好等于阈值放行、超 1 秒转人工） |
| `ConflictTests` | 冲突记录：敏感转人工、普通拒绝；`supersedes` 链消解冲突后正常放行 |
| `NormalRequestTests` | 业务范围允许/拒绝；业务不要求在岗时放行；过期仅留咨询标记不阻断；无记录时敏感转人工、普通拒绝 |
| `TimeBoundaryTests` | 生效时间当刻生效、前 1 微秒不生效；接收时间当刻可见、前 1 微秒不可见；已接收但未生效的记录不适用；朴素时间被拒绝 |
| `ReviewAndCorrectionTests` | 迟到更正对原决定不可见、复核时 REINFORCED；无变化复核 UPHELD；历史更正 OVERTURN 普通拒绝；更正为离岗 OVERTURN 原放行；迟到更正与迟到普通更新的交错可见性 |
| `LedgerIntegrityTests` | 重复目录记录/重复请求 id 被拒绝；指纹在输入不变时稳定、新记录到达后变化；未知资源与普通请求缺 business_line 被拒绝 |

用例同时包含**可接受结果**、**应被阻止/拒绝的结果**，以及影响二者的**边界与交错场景**
（生效/接收/新鲜度边界、迟到更新与历史更正的交错）。

## 范围说明

本模块仅在本地工作区内实现与核验，全部夹具为测试内合成数据；不包含任何真实目标
探测、网络访问或攻击性行为。
