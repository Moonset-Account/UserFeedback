# 账户恢复会话模块

实现“带时限冷静期的账户恢复”流程：申请经过冷静期后，用户凭一次性恢复码
重新登记第二因素；模块完整记录旧设备确认、用户撤回、恢复码使用和重新登记
结果四类事实，并保证撤回即时作废、替换范围受限、并发提交只成功一次、请求
编号重试取回原决定。

仅依赖 Python 3.11 标准库，无第三方依赖。

## 目录结构与实际入口

```
workspace/
├── recovery/
│   ├── __init__.py      # 公开 API 汇总（RecoveryManager / RecoverySession 等）
│   ├── errors.py        # RecoveryError 及拒绝/状态/未知会话异常
│   ├── factors.py       # Factor 数据类与 FactorRegistry 第二因素登记簿
│   ├── session.py       # 核心状态机、事件流、幂等决定缓存、RecoveryManager
│   └── demo.py          # 可运行演示（可控时钟，无需真实等待）
└── tests/
    ├── __init__.py
    └── test_recovery_session.py  # unittest 验收用例（16 个）
```

* 库入口：`recovery` 包，主要类是 `recovery.RecoveryManager` 与
  `recovery.RecoverySession`。
* 演示入口（工作区根目录执行）：
  ```bash
  python3 -m recovery.demo
  ```
* 测试入口（工作区根目录执行，均为真实断言）：
  ```bash
  python3 -m unittest discover -s tests -t . -v
  # 隔离模式（忽略 PYTHON* 环境变量与用户 site 包）同样通过：
  python3 -I -m unittest discover -s tests -t . -v
  ```

## 核心模型

### 会话状态（`SessionStatus`）

`pending`（冷静期中）→ `active`（冷静期结束）→ `recovered`（成功终态）；
任意未完成时刻可转入 `withdrawn`（撤回终态）；超过会话有效期仍未完成则为
`expired`（过期终态）。三个终态之间不可互相转换。

### 事件流（只追加，`RecoverySession.events`）

`created` / `old_device_confirmed` / `withdrawn` / `recovery_code_used` /
`reenrollment_rejected` / `reenrolled` / `expired`，事件带连续序号、时间戳
和明细，覆盖题面要求记录的四类事实。

### 提交决定（`Decision`，按请求编号缓存）

每次 `submit_recovery`（成功或拒绝）都会把不可变 `Decision` 以
`request_id` 为键缓存；相同请求编号的任何重试直接返回同一对象，不再执行
第二次替换。可通过 `get_decision(request_id)` 主动查询。

## 规则与实现对应关系

| 题面约束 | 实现位置与做法 |
| --- | --- |
| 带时限冷静期，期内不能重新登记 | `submit_recovery` 比较 `now < created_at + cooldown_seconds`，拒绝原因 `cooldown_active` |
| 记录旧设备确认/撤回/恢复码使用/重新登记结果 | `RecoverySession._record` 只追加事件，对应 4 类 `EventType` |
| 撤回在重新登记前立即作废，原有因素继续可用 | `withdraw()` 置终态并记事件，不调用登记簿；后续提交统一以 `withdrawn` 拒绝 |
| 恢复成功只替换申请指定因素 | 提交时校验 `new_factor.factor_id == target_factor_id`，否则 `target_factor_mismatch`；`FactorRegistry.replace_factor` 只停用目标因素，其他因素不动 |
| 两次并发提交只能成功一次 | 会话内 `RLock` 把“校验+落决定+替换”串行化；成功者转终态，后来者得到 `session_in_final_state` / `recovery_code_already_used` |
| 同请求编号超时重试取回原决定 | 锁内先查 `_decisions[request_id]`，命中即原样返回；拒绝决定也缓存（条件事后满足仍回放原拒绝） |
| 一次性恢复码 | 恢复码仅存 SHA-256 摘要，常量时间比较；成功使用后置 `_code_used`，重复使用被拒 |
| 会话最长有效期 | 超过 `validity_seconds` 的提交触发 `expired` 事件并以 `session_expired` 拒绝 |

## 测试验证范围（16 个 unittest 用例）

**可接受结果**

* `test_happy_path_replaces_only_target_factor`：冷静期 + 旧设备确认后成功，
  目标因素被替换、旧因素停用留痕、非目标因素保持可用。
* `test_cooldown_boundary_just_before_and_exactly_at`：冷静期临界内侧拒绝、
  临界点恰好放行。

**应被阻止/拒绝的结果**

* 冷静期内提交（且不消耗恢复码、不改因素）；
* 未做旧设备确认即提交；
* 恢复码错误（不消耗码，新请求仍可成功）；
* 重新登记前撤回，撤回后提交被拒、因素不变、终态动作被阻止；
* 申请替换非指定因素被拒；
* 越过有效期后提交被拒并落 `expired` 事件；
* 成功后第二个请求编号再提交被拒，且使用/登记事件均只有一条。

**边界与交错场景**

* 有效期临界点（恰好到期可成功，超 1 秒即过期）；
* 同请求编号重试返回同一成功决定对象，事件不重复；
* 冷静期内的拒绝决定被“钉住”，条件满足后同编号重试仍回放原拒绝，换新编号
  才按当前状态处理；
* 4 线程不同请求编号并发：恰好 1 个成功、3 个被拒；
* 4 线程相同请求编号并发：拿到同一个决定对象，恢复码只使用一次；
* 完整审计事件流的顺序、序号连续性与时间戳单调性；
* 撤回幂等、管理器查找未知会话与入参校验。

## 设计说明与边界

* 时间来源通过可注入的 `clock` 提供（`ManualClock` 供测试/演示使用，
  `RecoveryManager` 默认使用 `time.time`），测试无需真实 sleep。
* 本实现为进程内内存模型（会话、决定、因素登记簿均驻留内存并自带线程锁），
  不落盘、不联网；多进程部署需要把会话存储与决定缓存换成带唯一约束的共享
  存储，接口形状可保持不变。
* 恢复码以 SHA-256 摘要保存并以 `hmac.compare_digest` 比较；演示中的恢复码
  仅为示例，生产场景应由安全随机源生成并通过带外渠道下发。
