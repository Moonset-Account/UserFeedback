# 字段血缘分级与降级审批（SEC-20261114-024）

共享表管理员可用本工具回答：“这个衍生字段为什么继承了更高的数据分级？”
工具以本地 JSON 清单为唯一输入，计算每个字段的数据分级并给出一条解释来源；
支持撤销某条降级批准后重新载入清单，列出因此受影响的下游字段。

仅使用 Python 3 标准库，无第三方依赖，不联网。

## 本地入口（可重跑）

```bash
# 1. 按清单计算分级与解释
/usr/local/bin/python3 app.py classify samples/manifest.json

# 2. 撤销某条降级批准后重新载入，列出受影响的下游字段
/usr/local/bin/python3 app.py revoke samples/manifest.json DG-1

# 3. 本地核验（单元测试 + 样例逐条断言，有限时间内退出）
/usr/local/bin/python3 tools/local_verify.py
```

两个子命令都支持 `--out FILE` 把 JSON 结果同时写入文件。

## 输入格式（清单 JSON）

```json
{
  "levels": ["S1", "S2", "S3", "S4"],
  "fields":     {"users.ssn": {"level": "S4"}},
  "edges":      [{"from": ["users.ssn", "users.name"], "to": "mart.customer_profile"}],
  "downgrades": [{"id": "DG-1", "output": "mart.customer_profile",
                  "inputs": {"users.ssn": "S4"}, "target": "S2",
                  "status": "approved", "reason": "脱敏后输出"}]
}
```

- `levels`：等级序列，按从低到高排列；缺省 `["S1","S2","S3","S4"]`。
- `fields`：原始字段及其等级（`{"名": {"level": "Sx"}}` 或 `{"名": "Sx"}`）。
- `edges`：跨表派生关系，`from` 为输入字段列表，`to` 为输出字段。
- `downgrades`：已批准的降级规则。`output` 是具体输出字段，`inputs` 是
  “输入字段 → 批准时的输入等级”映射，`target` 是允许降到的目标级别，
  `status` 为 `approved`（缺省）或 `revoked`。

## 输出语义

`classify` 输出 JSON 报告：

- `fields[名].level`：字段分级；无法确定时为 `null`。
- `fields[名].status`：`ok` 或 `pending_review`（待核对）。
- `fields[名].in_cycle`：字段是否处于血缘环中。
- `fields[名].explanation`：一条解释来源（继承自哪个输入 / 按哪条规则降级 /
  为何待核对）。
- `cycles`：环涉及的字段分组（强连通分量）。
- `pending_review`：全部待核对字段。
- `downgrades`：`applied`（已生效规则）、`unmatched`（批准范围不匹配的规则及
  原因）、`revoked`（已撤销规则）。

`revoke` 输出：`revoked_rule`、`affected_count`、`affected_fields`
（每个受影响字段的 before/after 等级与状态）。

分级规则：

1. 一般派生取输入中的最高等级。
2. 仅当某条 `approved` 降级规则的批准范围完全匹配——输出字段相同、且规则
   列出的每个输入字段当前等级等于规则记录等级——才把输出降到 `target`。
   不匹配的规则不生效，并在 `unmatched` 中给出原因。
3. 血缘缺边（字段无原始等级也无血缘边）标记为 `pending_review`，并沿血缘
   向下游传播。
4. 环中的等级从最低级开始迭代到稳定（取值有界、单调不减，必然收敛），
   环内字段在 `cycles` 中报告。
5. `revoke` 将指定规则置为 `revoked` 后重新计算，以“等级或状态发生变化”
   的字段作为受影响的下游字段。

## 需求 → 测试/样例映射

| 原始需求 | 对应核验 |
| --- | --- |
| 一般派生取输入最高等级 | 单测 CASE-MAX-INHERIT；样例 SC-05 |
| 仅记录在案的降级可降到目标级别 | 单测 CASE-DG-APPLIED；样例 SC-01/SC-02 |
| 批准范围必须匹配具体输出字段和输入等级 | 单测 CASE-DG-SCOPE；样例 SC-03/SC-04 |
| 血缘缺边标记待核对（含向下游传播） | 单测 CASE-PENDING-MISSING-EDGE、CASE-PENDING-PROPAGATION；样例 SC-06/SC-07 |
| 环中等级迭代到稳定并报告涉及字段 | 单测 CASE-CYCLE-STABLE；样例 SC-08/SC-09 |
| 按输出字段给出分级和一条解释来源 | 单测 CASE-EXPLANATION；样例 SC-10 |
| 撤销批准后重新载入，列出受影响下游字段 | 单测 CASE-REVOKE-IMPACT、CASE-SAMPLE-E2E；样例 SC-11/SC-12/SC-13/SC-14 |
| 非法清单被拒绝 | 单测 CASE-MANIFEST-VALIDATION |

## 限制

- 等级模型是全序列表（`levels` 顺序即高低顺序），不支持格/部分序。
- 字段同时有原始等级和血缘边时，以血缘边（派生语义）为准。
- 多条规则同时命中同一输出字段时，取目标等级最低者，并列取规则 id 较小者。
- 撤销影响按“分级/状态实际变化”判定；未发生变化的下游字段不列出。
- 单文件清单、全内存计算，面向离线合成输入；不连接任何真实云、身份或
  生产系统。
