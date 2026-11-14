# SEC-VULN-033 会话代次参考模型（会话轮换与旧状态失效回归）

纯 Python 3 标准库实现的会话代次（session generation）参考模型，用于评估
“登录后会话存储方式修改”中的状态转换与提交规则。外部系统（主节点、读副本）
均以内存适配器模拟，无任何网络、依赖或真实凭据。

## 模型语义

### 状态转换（每次成功提交使会话代次 +1）

| 操作    | 前置状态      | 后置状态      | 含义                     |
|---------|---------------|---------------|--------------------------|
| LOGIN   | ANONYMOUS     | AUTHENTICATED | 匿名会话转为已登录会话   |
| ELEVATE | AUTHENTICATED | ELEVATED      | 同一身份提升权限         |
| LOGOUT  | AUTHENTICATED / ELEVATED | LOGGED_OUT | 主动退出（终态） |

### 提交规则

1. 只有主节点应用成功的转换才分配逻辑序号 `seq` 与新代次；
2. 写入确认（ack）丢失不代表未提交，以主节点日志为准；
3. 同一 `(session_id, op_id)` 的重试幂等：返回首次提交结果，不重复换发代次；
4. 读副本按 `seq` 顺序应用日志；对尚未复制到的代次必须安全拒绝
   （`DENY_UNKNOWN_SESSION`）或返回待确认（`PENDING_REPLICATION`），不得放行。

### 决策记录字段

每条决策记录包含：逻辑顺序 `order`、场景、动作、节点、会话、操作 `op_id`、
会话代次 `generation`、写入确认 `write_ack`、读副本版本 `replica_version`、
访问判定 `verdict`、备注。

### 回归规则

| 规则 | 内容 |
|------|------|
| R1 | 新代次提交并复制到某节点后，该节点不得再放行旧代次（及时失效） |
| R2 | 同一操作重试必须幂等，不得重复换发新代次 |
| R3 | 缺失写入确认/未复制时，副本必须安全拒绝或返回待确认 |
| R4 | 通过校验的新会话代次必须仍可正常访问 |
| R5 | 注销提交并复制后，任何代次都不得再被放行 |

失败记录关联规则号、场景、决策序号、提交确认、读副本版本与会话代次。

## 目录结构

```
app.py                      CLI 主入口
boundary_checks.py          本题样例/边界断言（BC-01 ~ BC-18，含实际值与期望值）
session_model/
  model.py                  状态机、提交规则、内存主节点 Primary 与读副本 Replica
  scheduler.py              确定性调度器（逻辑时刻+优先级，无随机/墙钟）
  scenarios.py              四个确定性场景
  regression.py             R1~R5 回归规则校验
tests/                      unittest 套件（33 项）
tools/local_verify.py       本地核验包装器
```

## 本地可重跑入口

```bash
# 一键核验：unittest 发现/执行计数 + 样例边界断言，全部通过退出 0
/usr/local/bin/python3 tools/local_verify.py

# 运行全部场景并输出回归报告（任一规则非 PASS 退出 1）
/usr/local/bin/python3 app.py run
/usr/local/bin/python3 app.py run --scenario retry_after_interruption
/usr/local/bin/python3 app.py run --json

# 逐条样例/边界断言（含实际结果）
/usr/local/bin/python3 app.py boundary

# 查看规则与场景清单
/usr/local/bin/python3 app.py rules
/usr/local/bin/python3 app.py scenarios
```

## 场景（确定性调度，可重复）

- `normal_login`：正常登录；含匿名提权拒绝、匿名代次、未来代次、未知会话边界。
- `concurrent_reads`：登录(gen1)+提权(gen2) 后，最新副本与滞后副本在同一逻辑
  时刻并发受理不同代次请求：新代次在最新副本 ALLOW、旧代次 DENY_STALE；
  滞后副本对新代次返回 PENDING_REPLICATION。
- `retry_after_interruption`：写入已应用但确认丢失 → 同 op_id 重试幂等回放
  同一代次（不重复换发）；应用前中断 → 重试正常提交。确认缺失且未复制时
  副本安全拒绝。
- `late_logout`：注销提交后主节点立即拒绝；已复制注销的副本拒绝；注销事件
  迟到的滞后副本在复制前仍放行旧代次（信息性，不计违规），复制后拒绝。

## 输出语义

- 文本/JSON 报告：逐条决策记录 + 各规则 `PASS/FAIL/INSUFFICIENT_EVIDENCE`
  及失败明细；`总体: PASS` 时退出码 0，否则 1。
- `INSUFFICIENT_EVIDENCE` 表示该规则在轨迹中缺少判定证据（如 R3 无缺失确认
  的读取样本），按不通过处理。

## 限制

- 单进程内存模拟，不覆盖真实网络分区、时钟漂移与持久化崩溃恢复；
- 副本滞后期间的旧代次放行记录为信息性，不视为违规（规则按“已复制到该
  节点”判定及时失效）；
- 不使用 pip/venv/pytest 或任何第三方库；不访问外部系统。
