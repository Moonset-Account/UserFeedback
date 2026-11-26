# 离线值班交接传输审批

纯 Python 3.11 标准库实现。输入一份**已经完成密码学校验**的握手转录、发送/接收两端的可信时间区间或“观测时间 + 时钟误差”、两端公告的密钥录入窗口，以及最大送达延迟；输出密钥承诺是否存在一致的发送/接收时刻、确定可用时段、歧义时段和矛盾证据。

> 题面没有规定固定函数名或 JSON Schema，因此本仓库显式定义下面的公开 API 与输入格式。`solution.py`、`main.py`、`approval.py` 均提供常用导入别名。

## 1. 公开 API

```python
from transfer_approval import evaluate_case, approve_transfer, Interval

report = evaluate_case(case: dict) -> dict

report = approve_transfer(
    transcript=...,
    sender=...,
    receiver=...,
    max_delay=10,
    key_group="duty-handover-key",
) -> dict
```

别名：

- `evaluate_transfer` / `assess_transfer` / `solve` / `evaluate` 等价于 `evaluate_case`
- `check_transfer` / `check_approval` 等价于 `approve_transfer`
- `OfflineTransferApproval.evaluate_case(...)` 提供类门面
- 根目录 `solution.py`、`main.py`、`approval.py` 重新导出上述 API

CLI：

```bash
python3 -m transfer_approval examples/synthetic_case.json
python3 main.py examples/synthetic_case.json --compact
cat case.json | python3 -m transfer_approval --stdin
```

CLI 退出码：`0=certain`，`3=ambiguous`，`1=infeasible/contradictory`，`2=输入无法解析`。

## 2. 输入格式

时间轴可以是整数、浮点数或其他支持比较/加减的标量；同一用例内不要混用不可比较类型。默认用户给出的区间和窗口为闭区间 `[start, end]`，也支持端点开关。

```json
{
  "max_delay": 10,
  "key_group": "duty-handover-key",
  "transcript": {
    "validated": true,
    "commitments": [
      {
        "id": "send-commitment",
        "subject": "send",
        "key_group": "duty-handover-key",
        "window_id": "sender-intake",
        "interval": [1, 3]
      }
    ],
    "prior_windows": [
      {"id": "sender-intake", "side": "sender", "key_group": "duty-handover-key"}
    ],
    "recoveries": [
      {
        "id": "recovery-1",
        "inherits": {"sender": ["sender-intake"], "receiver": ["receiver-intake"]}
      }
    ],
    "supplemental_evidence": [
      {
        "id": "backup-clock",
        "commitment_id": "send-commitment",
        "interval": [2, 3]
      }
    ]
  },
  "sender": {
    "observed_at": 2,
    "clock_error": 1,
    "windows": [
      {"id": "sender-intake", "start": 0, "end": 6, "key_group": "duty-handover-key"}
    ]
  },
  "receiver": {
    "observed_at": 8,
    "clock_error": 1,
    "windows": [
      {"id": "receiver-intake", "start": 6, "end": 12, "key_group": "duty-handover-key"}
    ]
  }
}
```

支持的常见别名：

- 端：`sender`/`send`，`receiver`/`receive`/`recv`
- 时间区间：`trusted_interval`、`time_interval`、`interval`、`[lo, hi]`
- 时钟误差：数值表示正负对称误差；对象支持 `earlier/later`、`negative/positive`、`minus/plus`
- 窗口：`windows`、`entry_windows`、`intake_windows`、`announced_windows`
- 延迟：`max_delay`、`maximum_delay`、`max_delivery_delay`
- 补交证据：`transcript.supplemental_evidence`、`late_evidence`、`time_evidence`

端点开闭可通过 `start_inclusive` / `end_inclusive` 设置，也支持 `"endpoint_mode": "closed" | "open" | "half_open"`。

## 3. 判断规则

对每个主体（`send`、`receive`）：

1. 从时钟误差或直接可信区间得到初始时间声明。
2. 加入握手承诺中的时间区间和所有补交时间证据。
3. 所有一致声明的融合区间为它们的交集；两两不相交的来源进入 `source_contradictions`。
4. 仅选择密钥匹配且继承关系有效的公告窗口，求融合区间与窗口并集的交集。
5. 用区间 Minkowski 和计算延迟投影：
   - 接收时刻必须属于 `S + [0, max_delay]`
   - 发送时刻必须属于 `R + [-max_delay, 0]`
6. 若存在满足条件的 `(s, r)`，则 `feasible=true`，报告中给出一个可实际取样的 `delay.witness`。

延迟边界按“最多允许”包含等号处理，即 `r - s == max_delay` 可用。

### 连接恢复与继承

模块不会隐式继承任何窗口：

- 恢复事件必须在 `transcript.recoveries[].inherits` 中显式声明窗口 ID。
- 声明可用 `{"sender": [...], "receiver": [...]}`、全局 ID 列表，或 `"sender:window-id"` 形式。
- 被声明继承的窗口必须出现在 `transcript.prior_windows` 或先前承诺的 `window_id` 中。
- 端点窗口标记 `"inherited": true` 或提供 `recovery_id` 时，若转录没有对应声明，产生 `undeclared_window_inheritance` 矛盾。
- 引用不存在的先前窗口产生 `unknown_inherited_window`；端侧或密钥不一致也会产生矛盾。

### 补交证据一致性

补交证据若带有 `commitment_id`，会检查：

- 承诺 ID 是否存在；
- `subject/side` 是否一致；
- 密钥组/密钥集是否一致；
- 时间区间是否与此前承诺相交。

不相交即输出 `supplemental_time_conflict`，同时通用区间融合会输出 `time_evidence_conflict`。矛盾是阻断性的，安全默认不自动批准。

## 4. 输出结构

主要字段：

```text
status: "certain" | "ambiguous" | "infeasible" | "contradictory"
decision: "approve" | "manual_review" | "deny"
approved: 仅 status=certain 时为 true
feasible / can_hold: 是否至少存在一个一致的发送/接收赋值
certain: 是否对所有原始时钟不确定性都稳健成立

certain_periods / definite_usable_periods / valid_intervals
ambiguous_periods / ambiguous_intervals
guaranteed_periods / robust_periods
conflicts
source_contradictions / contradictory_evidence / conflicting_evidence

sender / receiver:
  fused_interval
  source_union
  partial_source_intervals
  accepted_intervals
  feasible_intervals
  delay_excluded_intervals
  outside_window_intervals
  window_guaranteed
  windows

delay:
  max_delay
  raw_possible_delay_bounds
  accepted_possible_delay_bounds
  guaranteed_nonnegative
  guaranteed_within_limit
  guaranteed
  witness

recoveries
```

时段对象保留端点语义：

```json
{"start": 3, "end": 7, "start_inclusive": true, "end_inclusive": false}
```

分类口径：

- `certain_periods`：经证据融合、密钥窗口和延迟投影后，**数学上确定属于某个可行赋值**的真实时段。
- `guaranteed_periods`：只有 `status=certain` 时非空；表示整个原始不确定区间都在窗口内且任意两端组合都满足延迟。
- `ambiguous_periods`：
  - `partial_source_coverage`：仅被部分时间来源覆盖；
  - `outside_announced_key_window`：时间证据允许、但不在对应密钥窗口内；
  - `delay_relation_not_guaranteed`：两端交错区间中仍可能出现负延迟或超延迟配对。
- `delay_excluded_intervals`：已经被窗口接受、但经延迟关系证明不可用的确定排除段，因此不放入歧义列表。
- `conflicts`：全部阻断问题；`source_contradictions` 只保留证据、转录、密钥来源矛盾（不含普通约束不可行）。

## 5. 本地验证

运行完整测试：

```bash
python3 -m unittest discover -s tests -v
```

当前验证环境为 Python 3.11.6。最近一次完整执行：

```text
Ran 40 tests in ...
OK
```

> 具体耗时以本机运行为准；上述计数来自新增 API/边界测试后的实际执行。若后续修改新增测试，计数可能增加。

测试全部使用本地合成数据，覆盖：

- 正常确定审批与关键字 API；
- 对称/非对称时钟误差；
- 最大延迟等号边界、超 1 拒绝、零延迟；
- 窗口起点/终点的闭区间与开区间边界；
- 发送/接收区间交错；
- 多个离散窗口与延迟投影裁剪；
- 补交证据重叠缩核、尾部歧义、完全 disjoint 矛盾；
- 密钥组推断、密钥不匹配、无共同密钥组；
- 恢复窗口合法继承、未声明继承、未知旧窗口、无隐式继承；
- JSON 可序列化和 CLI 成功/非法输入退出码；
- 区间交集、合并、差集、开端点和延迟 Minkowski 和。

也可手动运行合成样例：

```bash
python3 -m transfer_approval examples/synthetic_case.json
```

## 6. 范围与限制

- 本模块只实现题面要求的防御性审批/一致性判断，不包含任何攻击、利用、入侵或规避逻辑。
- “已经校验的握手转录”表示签名、哈希等密码学校验是前置条件；若显式传入 `"validated": false`，API 抛出 `TranscriptValidationError`。
- 不访问网络、不读取凭据、不上传材料、不安装依赖。
- 模块不选择或生成密钥，只判断声明的同一密钥组是否被两端对应窗口接受。
