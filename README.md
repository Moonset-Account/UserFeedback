# approval_compat — pre-release check for approval-bearing fields

一个纯本地、无第三方依赖的 Python 3.11 库，用于在**发布前**回答：

> 给内部消息新增一个有审批含义的字段后，固定的转发路线上，旧版发送者能否让自己的普通字段被新版解释为"已批准"？

库的输入是每一版消息的字段号、编码类别、业务含义，以及每条固定转发路线中每一跳的处置方式（重新生成 / 按原字节透传 / 移除）。输出是一条路线是否放行，以及**首次改变权限含义的版本和字段来源**。

不做任何网络访问、目标探测或攻击载荷生成；所有夹具都是本工作区里的合成数据。

## 结论规则（一句话）

**字段号会穿过任何"不认识"它的中继。** 因此只要某个字段号在旧版是普通字段、在新版是审批字段，旧版写入的字节就会在没有任何一跳改写它的情况下被读成"已批准"。中继把它当作未知字段，不是安全边界，而是传送带。

一条路线放行，仅当每个目的地审批字段满足以下之一：

| 到达状态 | 含义 | 结论 |
| --- | --- | --- |
| `absent` | 没有任何字节到达 | 放行（WARN：闸门无输入，仅当读取方把"缺失"当作"未批准"才安全） |
| `trusted` | 某跳丢弃旧字节后，从受信输入重建 | 放行（INFO，附重建跳名） |
| 全新字段号 | 旧版 schema 根本无法寻址该字段号 | 放行 |
| `sender_bytes` | 旧版发送者的字节原样到达审批字段 | **阻止** `legacy_ordinary_promoted` |
| `hop_written_unauthenticated` | 某跳重写了它，但来源不是受信输入 | **阻止** `unauthenticated_approval_value` |
| `duplicate_carrier` | 该跳无法解码此字段，只能追加一份副本，旧字节仍在线上 | **阻止** `duplicate_approval_carrier` |

另有两条不改变放行结论、但必须报出的诊断：

- `weak_approval_encoding`（BLOCK）：审批含义用 `oneof` / `repeated` / `packed` 承载，写入方自己选择分支或元素，也就自己选择了答案。
- `intermediate_permission_change`（WARN）：某中间版本把字段提升为审批、目的地又不再读它。目的地干净，但**固定在该中间版本的消费者**仍会把旧字节当同意。
- `preexisting_approval_channel`（WARN）：发送者本来就是有权限的审批写入方，这条路线既没制造也没消除该能力。

严重级别：`BLOCK` 阻止发布；`WARN` / `INFO` 不阻止，但会出现在报告和退出码之外的输出里。

## 实际入口

工作区根目录为 `/Users/xingyaolei/Desktop/feedback-studio/runs/feishu-nightly-20261120/UID-13965/round-1/attempt-1/workspace`。以下命令都在该目录执行。

**CLI（真实入口）**

```bash
.venv/bin/python -m approval_compat.check tests/fixtures/routes.json
.venv/bin/python -m approval_compat.check --json tests/fixtures/routes.json
```

- 输入：`{"routes": [...]}`，或单个路线对象，或路线数组。
- 输出：逐条路线的文本诊断；`--json` 输出 `{"summary": ..., "reports": [...]}`。
- 退出码：`0` 全部放行；`1` 至少一条路线被阻止；`2` 输入无法解析或夹具自相矛盾。
- 因为从包内以模块方式运行，`.venv/bin/python -m approval_compat.check` 需要在工作区根目录下执行（当前目录即包所在目录）。

**测试**

```bash
.venv/bin/python -m unittest discover -s tests -t tests -v
```

`-t tests` 让 `tests/` 成为顶层目录，使 `support.py` 可被直接导入；测试入口全部为 `unittest` 真实断言（`assertTrue/assertFalse/assertEqual/assertIn/assertRaises`），没有 print-only 或跳过用例。

**库 API**

```python
from approval_compat import MessageVersion, HopTransform, Route, check_route

v1 = MessageVersion.from_raw("v1", {7: ("normal", "display_name")})
v2 = MessageVersion.from_raw("v2", {7: ("normal", "approval_decision")})
report = check_route(Route(
    name="legacy-direct",
    origin=v1,
    destination=v2,
    hops=(HopTransform(hop="relay"),),   # 中继不认识第 7 号字段，原样透传
))
assert not report.ok
print(report.format_text())
```

## 输入格式

```jsonc
{
  "routes": [
    {
      "name": "route-1",
      "origin":      {"version": "v1", "fields": {"7": {"encoding": "normal", "meaning": "display_name"}}},
      "destination": {"version": "v2", "fields": {"7": {"encoding": "normal", "meaning": "approval_decision"}}},
      "timeline":  [ /* 可选：从 origin 到 destination 的完整发布序列，用于定位中间版本 */ ],
      "senders":   [ /* 可选：真正喂这条固定路线的客户端版本，缺省为 [origin] */ ],
      "legacy_unknown_field_bytes": false,
      "hops": [
        // 只认识 1、7 号；7 号删掉旧值并从受信输入重建
        {"hop": "core", "known_numbers": [1, 7], "regenerate": [7],
         "rebuilt_from": {"7": "trusted"}},

        // 不认识任何字段，未知字段原样透传（默认）
        {"hop": "edge"},

        // 未知字段整体丢弃
        {"hop": "gateway", "unknown_bytes": "dropped"},

        // 明确移除某字段号
        {"hop": "scrubber", "drop": [7]}
      ]
    }
  ]
}
```

字段语义：

- `encoding`：`normal` | `packed` | `oneof` | `repeated`。
- `meaning`：自由字符串，大小写不敏感。审批含义为 `approval`、`approval_decision`、`approval_scope`、`approver_identity`；其它一律视为普通字段。库**不会**根据字段名猜含义，必须显式声明。
- 每跳三桶 `regenerate` / `passthrough` / `drop` 互斥，重叠会抛 `SchemaError`。
- `rebuilt_from[字段号]`：`trusted` / `trusted_input` / `untrusted`（缺省按 `untrusted` 处理）。只有 `trusted*` 赋予审批权威。
- `known_numbers`：该跳自己的 schema 声明了哪些字段号。缺省 `null` 表示"什么都不认识"（不透明中继）。**只有能解码某字段的中继才能替换它**；不认识却声明 `regenerate` 会被判定为"旧字节仍在线上"的 `duplicate_approval_carrier`。
- `legacy_unknown_field_bytes`：`true` 时按更严格的威胁假设——旧版编码器还可能写出自己 schema 未声明的字段号。缺省 `false`，即"旧版能写的普通字段"只指它声明过的字段。

## 验证范围

`tests/` 下 53 个用例，分四个模块，覆盖题面的交叉约束：

| 文件 | 覆盖 |
| --- | --- |
| `test_accepted_routes.py` | 可接受结果：能解码的跳丢弃旧值并从受信输入重建；两跳分担"先丢弃、后重建"；透传后再重建；`unknown_bytes="dropped"` 全程丢弃；全新字段号；同一路线里既有被批准的重建、又有被阻止的旧字段（混合交错）。 |
| `test_blocked_routes.py` | 应拒绝结果：普通字段经 1 跳 / 3 跳 / 100 跳未知中继后抵达审批字段；`passthrough` 不能洗白；无受信来源的 `regenerate`；缺省来源按不可信处理；不透明中继追加副本；丢弃后又用不可信来源重写；审批字段用 `oneof` / `repeated` / `packed` 承载。 |
| `test_boundary_cases.py` | 影响两侧的边界与交错：字段号退休后复用；多个发送者时按最弱发送者判定；仅中间版本提升；同一路线形状因一个标志位分别放行/阻止；"重建→透传"与"透传→丢弃"的顺序差异；trace 状态与结论一致。 |
| `test_diagnostics_and_entrypoint.py` | 诊断内容（首次权限变更版本、字段来源、携带路径跳名）、JSON 报告字段、CLI 退出码 0/1/2、`python -m` 子进程入口、夹具自洽性、schema 校验报错。 |

混合交错的验收点（路线中既有被批准的重建，又有被阻止的旧字段）由 `tests/fixtures/routes.json` 与 `test_one_bad_field_does_not_hide_a_good_rebuild` 承担：6 条固定路线中 3 条放行、3 条阻止，测试同时断言两种结论都出现。

## 限制

- 不解析真实 protobuf 字节，只按调用方声明的字段号/编码/含义做语义推演。输入的准确性由使用方负责。
- `timeline` 缺省时退化为 `(origin, destination)`，此时无法发现"仅中间版本提升"的情况——要检测它必须显式给出时间线。
- 未对嵌套消息、map 字段、`Any` 装箱建模；这些应作为独立路线描述后分别检查。
