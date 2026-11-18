# 补丁重启批次编排组件（Reboot Orchestrator）

办公计算集群安装安全补丁后，必须等待承载进程重启并加载新版本，主机才能退出
"待防护"状态。本组件依据**主机安装回执、进程加载版本、重启容量、服务副本分布**
四类输入，完成重启名额的预留、下发、超时回收与补丁生效确认。

## 目录与入口

```
workspace/
├── orchestrator.py            # 组件实现，无第三方依赖，仅用标准库
├── run_tests.py               # 测试运行入口（支持 python -I 隔离模式）
├── tests/
│   ├── __init__.py
│   └── test_orchestrator.py   # unittest 验收用例（13 个）
└── README.md
```

测试入口（两者等价，均执行真实断言）：

```bash
python3 -I run_tests.py                 # 隔离模式，不依赖当前工作目录
python3 -m unittest discover -s tests   # 标准 unittest 发现
python3 -m unittest tests.test_orchestrator.RebootOrchestratorTests.test_happy_path_accept
```

运行环境：Python 3.11（实测 3.11.6），标准库 `dataclasses / enum / unittest`，
无需联网或安装依赖。

## 核心 API（`orchestrator.py`）

| 方法 | 职责 |
| --- | --- |
| `add_replica(host, service, process)` | 登记服务副本分布（主机上的进程承载哪个服务） |
| `begin_batch(batch_id=None)` | 开启新批次；全部主机回到无回执状态并释放名额/计时 |
| `submit_receipt(InstallReceipt)` | 接收安装回执；**非当前批次的迟到回执拒绝** |
| `reserve_slot(host_id)` | 预留重启名额（计入容量，不触发重启、不做副本校验） |
| `dispatch(host_id, timeout_seconds)` | 原子校验回执/容量/健康副本后下发重启并开始计时 |
| `report_version(VersionReport)` | 接收进程版本上报；全部进程加载目标版本才生效 |
| `expire_timeouts()` | 回收超时名额，主机保持 `PENDING_CONFIRMATION` |
| `get_state / held_slots / effective_hosts / healthy_replica_count` | 状态与容量查询 |
| `events` | 结构化决策日志（接受、阻止、超时、生效均留痕） |

## 状态机

```
NO_RECEIPT ──当前批次回执──▶ AWAITING_DISPATCH ──reserve──▶ RESERVED
                                   │                          │
                                   └──────── dispatch ────────┘
                                                    │
                                                    ▼
                                                REBOOTING ──超时──▶ PENDING_CONFIRMATION
                                                    │  ▲                    │
                                       版本上报(部分) ┘  └──── 重新下发 ◀─────┘
                                                    │
                                          全部进程加载目标版本
                                                    ▼
                                                EFFECTIVE（释放名额）
```

## 交叉约束与实现位置

1. **回执门槛**：仅当前批次回执可进入编排；旧批次迟到回执返回
   `STALE_RECEIPT`（`submit_receipt`）。
2. **容量约束**：名额 = 容量 −（预留中 + 重启中主机）；满载返回
   `CAPACITY_FULL`，预留与重启共同占用名额。
3. **健康副本约束**：下发前对候选主机承载的**每个**服务做预判，扣除预留/
   重启/候选主机后健康副本仍须 ≥ `required_healthy`，否则返回
   `QUORUM_BREACH`，且不占用任何名额。
4. **超时回收**：`expire_timeouts()` 释放重启名额，主机回到
   `PENDING_CONFIRMATION`（待确认），后续可凭版本上报生效或重新下发。
5. **生效条件**：主机登记的**全部进程**都上报当前批次目标版本才置
   `EFFECTIVE`；版本不符返回 `VERSION_MISMATCH`，部分上报只到待确认。
6. **批次隔离**：版本上报的 `batch_id` 必须等于当前批次，旧批次迟到上报
   返回 `WRONG_BATCH`，不能完成新批次；`begin_batch` 重置全部运行态。

## 验收范围（tests/test_orchestrator.py，13 用例）

### 可接受结果
- `test_happy_path_accept`：回执 → 下发 → 全进程上报 → 生效并释放名额
- `test_reserve_then_dispatch`：先预留再下发，预留计入容量
- `test_quorum_exact_boundary`：健康副本恰好等于下限（2/3 → 2）允许下发
- `test_timeout_frees_slot`：超时释放名额后同主机可重新下发

### 应被阻止 / 拒绝
- `test_dispatch_blocked_without_receipt`：无回执不得下发（`NO_RECEIPT`）
- `test_dispatch_blocked_by_capacity`：容量满载阻止下发且不占名额
- `test_dispatch_blocked_by_quorum`：副本击穿下限阻止下发（`QUORUM_BREACH`）
- `test_stale_receipt_rejected`：新批次拒绝旧批次迟到回执
- `test_wrong_batch_report_rejected`：旧批次迟到版本上报不能完成新批次
- `test_wrong_version_keeps_pending`：版本不符拒绝；多进程缺一不可生效
- `test_unknown_host_rejected`：未登记拓扑的主机全部操作拒绝

### 边界 / 交错
- `test_interleaved_capacity_quorum_timeout`：4 主机 2 服务交错调度——
  先因副本不足阻止 h2，容量占满后 h1 超时回收，h4 版本确认恢复副本，
  h2 才具备下发条件，最后逐进程确认生效，验证容量、副本、超时三条约束联动。
- `test_event_log_records_decisions`：接受与阻止决策均写入审计事件流。

## 设计边界

- 组件为单进程内存态编排器，通过注入 `now_func` 实现确定性的时间/超时测试；
  未引入线程与持久化，集群侧适配（回执/上报通道、实际重启执行）不在本组件内。
- 所有数据均为本地合成夹具，测试不访问任何真实主机或外部服务。
