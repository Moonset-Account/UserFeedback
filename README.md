# 修复动作协调模块（remediation_coordinator）

值班系统根据观测生成修复动作后，本模块负责**只推导动作与结算回执**：
判断动作能否占位/派发、重新核对前置条件是否过期、计算全链路（含间接）
冲突、用代次（generation）结算执行器回执，并在进程重启后恢复在途动作。
模块不执行任何真实修复，执行器由调用方注入。

## 设计语义与题面对应

| 题面要求 | 实现位置与语义 |
| --- | --- |
| 输入为资源版本、诊断证据、前置条件、读写资源、执行阶段 | `model.WorldState`（资源版本+证据）、`model.ActionSpec`（reads/writes/preconditions/phases/irrevocable_phase） |
| 规划后、开始前资源/诊断变化要重新核对并拒绝过期动作 | `engine.RemediationCoordinator.pump` 每轮扫描先对 planned/scheduled 头部执行 `_evaluate_preconditions`；失败立即 `REJECTED` 并中止整条链 |
| 无关变化不影响其他链 | 前置条件只引用自己钉住的资源/证据；`resource_changed/evidence_changed` 后重跑 pump，未引用变化对象的链状态不变（见 `test_unrelated_resource_change_does_not_touch_chain`） |
| 全链路间接冲突不能同时占位 | `_conflict_detail` 以链为节点建并查集：同链非终态动作天然合并，跨链读写/写写不兼容即连边；候选链分量出现其他链即拒绝占位，BLOCKED 回执区分直接/间接 |
| 已进入不可撤销阶段的动作继续等结果 | `IRREVOCABLE` 状态下 `cancel` 只落 `CANCEL_REJECTED` 回执；`report_phase/report_effective/report_failed` 照常结算 |
| 随后的互相覆盖动作按最新事实重新排定 | 在途动作结算后 pump 接棒后续动作，重新核对最新资源版本与证据，过期即拒绝（见 `test_evidence_cleared_mid_flight...`） |
| 执行器返回带代次的结果 | 每次派发分配单调递增 generation（栅栏令牌）；代次不匹配的回执只记 `STALE_RESULT`，状态与世界版本不变 |
| 重启区分待开始/已生效未确认/尚可取消 | `recovery_buckets()` 四桶分类 + `to_dict/restore`、`save_snapshot/load_snapshot` JSON 快照 |
| 两条修复链共用热备、一条告警中途解除 | `tests/test_hot_standby_scenario.py` 端到端核对处置记录与允许发出的后续动作 |

### 动作状态机

```
planned ──> scheduled ──> dispatched ──> irrevocable ──> effective ──> confirmed
   │            │             │                              ▲
   │            │             └──> cancelling ──> cancelled  │（取消迟到，生效优先）
   ▼            ▼             └──> failed                    │
rejected   cancelled            （dispatched/irrevocable/cancelling 均可失败）
```

- **待开始**：`planned`（已推导未占位）、`scheduled`（已占位未派发；未注入执行器时停在此态）。
- **尚可取消**：`dispatched`（未越过 `irrevocable_phase`）、`cancelling`（取消请求在途）。
- **不可撤销在途**：`irrevocable`，只能等执行器结果。
- **已生效未确认**：`effective`，只能 `confirm`。
- 终态：`confirmed / cancelled / rejected / failed`。

## 公开 API

```python
from remediation_coordinator import (
    # 世界与事实
    WorldState, Evidence, ResourceVersion, EvidenceActive, EvidenceCleared, AllOf,
    # 动作规格与状态
    ActionSpec, ActionState, AccessMode,
    # 协调器
    RemediationCoordinator, RecoveryBuckets,
    # 执行器注入
    Executor, DispatchOrder, ScriptedExecutor,
    # 回执
    Receipt, EventType,
    # 持久化
    save_snapshot, load_snapshot,
)
```

### 核心方法

- `RemediationCoordinator(world, executor=None, clock=0)`
- `submit_chain(chain_id, specs)`：提交一条有序修复链（step 从 0 连续编号），自动 pump。
- `pump()`：扫描所有链，推进可占位/可派发的头部，重新核对前置条件与冲突。
- `resource_changed(resource_id, new_version)` / `evidence_changed(evidence_id, active, ...)`：
  登记外部事实变化（版本必须严格递增），随后自动重新核对。
- `cancel(action_id)`：待开始动作直接取消并中止链；可撤销窗口内向执行器发取消；
  不可撤销/已生效/终态动作返回 `CANCEL_REJECTED` 回执。
- `report_phase(action_id, generation, phase)`：阶段推进，进入 `irrevocable_phase` 时状态转 `irrevocable`。
- `report_effective(action_id, generation, resource_versions=None)`：动作生效，可带回资源新版本。
- `confirm(action_id, generation)`：确认生效动作。
- `report_cancelled(action_id, generation)` / `report_failed(action_id, generation, reason)`。
- `recovery_buckets()`：返回 `pending_start / effective_unconfirmed / cancellable / irrevocable_in_flight`。
- `to_dict()` / `RemediationCoordinator.restore(data, executor=None)`；
  `save_snapshot(coordinator, path)` / `load_snapshot(path, executor=None)`（原子写入）。
- 查询：`receipts_for(action_id)`、`receipts_for_chain(chain_id)`、`conflict_components()`。

## 复现步骤

环境要求：Python 3.11.6，仅标准库，无第三方依赖。

```bash
# 在工作区根目录执行（-I 隔离模式，避免加载工作区外的 site 包）
python3 -I -m unittest discover -s tests -t . -v
```

`-t .` 把工作区根目录加入 `sys.path`，使 `remediation_coordinator` 包与
`tests` 包均可被发现；测试可从根目录直接发现，无需安装。

快照落盘的手工验证方式见 `tests/test_restart_recovery.py`，使用
`tempfile.TemporaryDirectory` 写本地 JSON 再 `load_snapshot` 恢复。

## 验证范围（共 36 个 unittest 用例，全部真实执行）

- `tests/test_hot_standby_scenario.py`（3）：双修复链共用热备端到端；
  无关变化隔离；无执行器时占位待开始遇证据解除被拒。
- `tests/test_conflicts.py`（5）：X–Y–Z 桥接间接冲突、桥退出后分量拆开、
  读写/写写直接冲突、读读兼容、BLOCKED 回执去重。
- `tests/test_staleness_and_generation.py`（11）：待开始动作资源过期拒绝、
  在途动作不被外部变化撤销、告警中途解除后的在途结算与后续拒绝、
  过期代次回执丢弃、不可撤销拒绝取消、取消窗口成功/取消迟到、
  阶段顺序校验、版本单调校验、失败中止链。
- `tests/test_restart_recovery.py`（7）：四桶分类、scheduled 归桶、
  快照往返保留状态/代次/账本、重启后旧代次拒绝、cancelling 恢复后结算、
  重启后代次继续单调、恢复后按最新事实拒绝被挡头部。
- `tests/test_boundary.py`（10）：版本单调、规格校验、链提交校验、
  非法状态回报、前置条件谓词与序列化、模型往返、取消去重、终态取消拒绝、
  回执序号严格递增、快照格式守卫。

未覆盖声明：模块不接入真实基础设施，所有"执行器"均为
`ScriptedExecutor` 本地假实现；未做性能压测与跨进程并发压测，
并发交错通过单线程内的确定性状态交错测试表达。
