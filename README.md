# 流计算会话聚合增量算子

按设备聚合会话的流式算子：同一设备的事件按**事件时间**排序，相邻事件间隔不超过
`10` 秒即属于同一会话。算子接受三种输入，并向下游发出 changelog：

| 输入 | 形式 | 说明 |
| --- | --- | --- |
| 插入 | `insert(event_id, device, time, amount)` | 编号在算子生命周期内全局唯一 |
| 撤回 | `retract(event_id)` | 按编号撤回此前插入的事件 |
| 水位 | `advance_watermark(value)` | 单调不减 |

每个会话结果 `SessionResult` 包含：设备、首时间、末时间、事件条数、金额和。
changelog 只有两种动作：`retract`（撤回旧结果）与 `insert`（插入新结果），
同一批内**先全部撤回、再全部插入**。

## 入口

```bash
# 验收测试（真实断言，unittest）
python3 -m unittest discover -s tests -v

# 本地合成数据演示（题面场景 + 另一台设备作为旁观者）
python3 -m session_operator
```

无需安装依赖，Python 3.11 标准库即可；仓库根目录即包导入根，因此请在仓库根目录执行上述命令。

## 关键语义

- **会话边界**：相邻事件时间差 `<= 10` 秒为同一会话；恰好 `10` 秒仍属同一会话，
  `10.001` 秒即分成两个会话。会话封存线为 `last_time + 10`。
- **撤回桥接事件**：撤回一个中间事件可能把一个会话拆成两个，此时先撤回合并后的
  结果、再插入两个拆分后的结果。反过来重新插入桥接事件会先撤回两个结果、再插入
  合并后的结果——下游应先见合并、再见拆分（`python3 -m session_operator` 与
  `test_downstream_sees_merge_before_split` 覆盖）。
- **水位约束**：`time <= watermark` 的事件不再接受插入或撤回（抛
  `WatermarkViolation`）。水位单调不减，回退抛 `WatermarkRegression`。
- **封存**：只有当水位达到 `会话末尾 + 10`（`last_time + gap <= watermark`）时
  会话才最终封存，此后对其任何撤回抛 `SealedSessionViolation`（`WatermarkViolation`
  的子类）。仅"事件时间 <= 水位"不足以判定封存——判定用的是**所属会话的末尾时间**。
- **原子性**：任何被拒绝的操作都不改变算子状态（水位、事件、已发布结果）。
- **设备隔离**：一次操作只发出受影响设备的变更，其他设备不产生任何 changelog。
- **退出码**：`python3 -m session_operator` 正常结束返回 `0`；测试入口在任断言
  失败时以非零码退出。

## 验证范围

`tests/test_session_operator.py` 共 35 个用例，均为真实断言，覆盖：

1. **可接受结果**
   - 题面场景：按 `0`、`18`、`9` 秒加入三条再撤回 `9` 秒那条，逐批核对 changelog
     的完整内容（先两条撤回 + 一条合并插入，再一条撤回 + 两条拆分插入）。
   - 恰好 `10` 秒的边界仍合并、`10.001` 秒拆分、连环 10 秒跳跃形成单会话。
   - 乱序到达与负时间、自定义 gap、浮点金额求和容差。
   - 旁观设备 `B` 在 `A` 的合并/拆分过程中完全不受影响。

2. **应被阻止或拒绝的结果**
   - 重复编号（含跨设备重号）、未知编号、撤回已撤回的事件。
   - 事件时间 `<= 水位` 的插入、`<= 水位` 的撤回。
   - 会话封存后的撤回、水位回退、非法水位/时间/金额/gap 输入。
   - 每个拒绝用例都同时断言状态未被改动（失败即回滚）。

3. **影响上述两者的边界与交错场景**
   - 封存线 `last_time + gap` 的 `9.999` 与 `10.0`：前者未封存、后者恰好封存。
   - 会话末尾与事件时间不一致时（`0..16` 会话里的 `8` 秒事件），拒绝原因是水位
     而非封存；对照已封存会话报 `SealedSessionViolation`。
   - 原子批次 `insert(..., watermark=...)` / `retract(..., watermark=...)`：先推进
     水位再执行，失败时水位与事件一起回滚；成功时水位恰好封存旧会话。
   - 拆分后重新插入桥接事件（split → re-merge），以及由纯 changelog 回放的下游
     视图与算子重算结果逐批一致。
   - 多设备交错变更下，下游回放结果始终等于算子已发布结果。

## 目录

```
session_operator/__init__.py   对外导出
session_operator/model.py      Event / SessionResult / Change
session_operator/errors.py     拒绝类异常
session_operator/core.py       SessionOperator 实现
session_operator/__main__.py   python3 -m session_operator 演示
tests/test_session_operator.py 35 个验收用例
```
