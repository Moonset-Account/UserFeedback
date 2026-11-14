# 重登记设备卷加密与密钥托管合规核对工具

笔记本重装后沿用原资产编号，本工具以**本次登记批次**为准，核对每台设备应加密卷的
加密状态与当前恢复密钥的托管情况。仅使用 Python 3 标准库，全部输入为本地合成 JSON
文件，不访问任何真实云、身份或生产系统。

## 运行环境

- Python 3（开发验证使用 `/usr/local/bin/python3`），无第三方依赖。

## 快速开始

```bash
# 执行一次核对（报告打印到 stdout，或用 --output 写入文件）
/usr/local/bin/python3 app.py \
  --inventory samples/inventory.json \
  --reports samples/reports.json \
  --receipts samples/receipts.json \
  --check-time 2026-11-14T12:00:00Z

# 本地核验：单元测试 + 样例逐条断言
/usr/local/bin/python3 tools/local_verify.py
```

退出码：`0` = 核对流程成功执行（不代表所有设备合规）；`2` = 输入错误。
`tools/local_verify.py` 退出码 `0` 表示单测全部通过且样例断言全部一致。

## 输入格式

### 资产清单（--inventory）

```json
{
  "batch": "B2026-11",
  "assets": [
    {"asset_id": "A-001", "volumes": ["C:", "D:"]}
  ]
}
```

- 顶层 `batch` 为当前登记批次；单个资产可用 `"batch"` 字段覆盖。
- `volumes` 为该设备应加密的卷列表。

### 终端加密上报（--reports）

接受 `{"reports": [...]}` 或裸数组。每条上报：

```json
{"asset_id": "A-001", "batch": "B2026-11", "volume": "C:", "sequence": 5,
 "observed_at": "2026-11-14T08:00:00Z", "encrypted": true, "key_id": "KEY-A1C-2"}
```

- `sequence`：整数序号，同一（资产, 批次, 卷）内取最高序号的上报。
- `observed_at`：ISO 8601 观测时间（支持 `Z`，无时区按 UTC）。
- `encrypted`：布尔；`key_id`：当前恢复密钥标识。

### 恢复密钥托管回执（--receipts）

接受 `{"receipts": [...]}` 或裸数组。每条回执：

```json
{"asset_id": "A-001", "batch": "B2026-11", "volume": "C:", "key_id": "KEY-A1C-2",
 "received_at": "2026-11-14T08:05:00Z"}
```

`received_at` 可选；匹配判定只用 `asset_id`/`batch`/`volume`/`key_id` 四元组。

### 核对时间（--check-time）

ISO 8601，例如 `2026-11-14T12:00:00Z`。

## 核对语义

对每个（资产, 当前批次, 卷）：

1. 仅考虑**当前批次**的上报；取序号最高者。同一最高序号存在内容冲突
   （观测时间/加密状态/密钥标识任一不一致）→ `SEQUENCE_CONFLICT`（待复核）。
2. 卷级通过条件（全部满足）：
   - 最新上报 `encrypted = true`；
   - `observed_at` **严格早于**核对时间，且相差 **≤ 24 小时**（恰好 24 小时算通过）；
   - 存在同时匹配 资产 + 登记批次 + 卷 + **当前密钥标识** 的托管回执。
3. 密钥轮换后，上报中的 `key_id` 变为新密钥，旧密钥回执不再匹配，
   只有收到新密钥的匹配回执才能重新通过。

### 卷级状态（优先级从高到低）

| 状态 | 含义 |
|---|---|
| `MISSING_REPORT` | 当前批次内无该卷上报（缺上报） |
| `SEQUENCE_CONFLICT` | 最高序号上报内容冲突，待复核（序号冲突） |
| `UNENCRYPTED` | 最新上报显示未加密（未加密） |
| `STALE_REPORT` | 观测时间不在 (核对时间-24h, 核对时间) 窗口内（上报过期/时间异常） |
| `AWAITING_ESCROW` | 缺少匹配当前密钥标识的托管回执（等待托管） |
| `PASS` | 全部条件满足 |

### 输出

JSON 报告：`devices[]` 逐设备给出 `status`（全部卷 PASS 才为 PASS，否则 FAIL）、
逐卷结果（含采用的序号、观测时间、密钥标识、原因），`summary` 汇总设备与卷状态计数。

## 测试与核验

- `tests/`：unittest 单元测试，覆盖六种卷状态、24 小时边界（恰好 24h 通过、
  超出/等于核对时间失败）、最高序号选取、同序号冲突、批次隔离、密钥轮换
  （旧回执失效、新回执恢复通过）、设备汇总与输入校验。
- `samples/`：合成样例输入与 `expected.json` 期望（稳定 case_id）。
- `tools/local_verify.py`：discover 并实际运行全部单测（0 项或全跳过视为失败），
  随后真实调用 `app.py` 跑样例并逐条比对，实际结果留存于
  `samples/last_verify_results.json`。

## 限制

- 纯本地离线工具：不联网、不访问真实终端/密钥托管系统，外部系统以合成 JSON 模拟。
- 输入校验失败（缺字段、类型错误、非法时间）以退出码 2 拒绝，不产生部分报告。
- 同一卷多条上报仅在当前批次内比较；历史批次数据不参与判定。
