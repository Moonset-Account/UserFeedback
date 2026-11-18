# 邮件安全网关投递决策模块

接收上游验真组件产生的域认证结果（SPF / DKIM / DMARC）与每跳转发服务的
信任状态，按输入配置的**域对齐规则**和**发件域策略**，对每封邮件给出
`deliver`（投递）/ `quarantine`（隔离）/ `reject`（拒收）裁决，并返回
最终采用的那一条认证证据。

仅使用 Python 3.11 标准库，无第三方依赖。

## 目录与入口

```
workspace/
├── mail_security/
│   ├── __init__.py        # 对外 API 汇总
│   ├── models.py          # 枚举、不可变数据模型、域对齐基础函数
│   ├── engine.py          # 决策引擎 evaluate() 与原因码常量
│   ├── cli.py             # 命令行入口
│   └── __main__.py        # 支持 python -m mail_security
├── tests/
│   ├── test_delivery_decision.py  # 32 项 unittest 验收用例（含 CLI 外的全部规则）
│   └── test_cli.py                # CLI 真实子进程冒烟测试
└── examples/              # 本地合成 JSON 夹具（非真实流量数据）
    ├── config.json
    ├── message_direct.json
    └── payload_forward.json
```

### 编程入口

```python
from mail_security import evaluate, GatewayConfig, Message

config = GatewayConfig.from_dict(config_dict)
message = Message.from_dict(message_dict)
decision = evaluate(message, config)
print(decision.disposition, decision.evidence_used)
```

### 命令行入口

```bash
# 方式一：合并载荷，顶层含 "config" 与 "message" 两个键
python -m mail_security --payload examples/payload_forward.json

# 方式二：配置与消息分文件
python -m mail_security --config examples/config.json \
                       --message examples/message_direct.json
```

输出裁决结果 JSON（见下文字段）。正常裁决（含隔离/拒收）退出码为 `0`；
输入文件缺失、JSON 非法或 schema 校验失败时退出码为 `1` 并向 stderr
输出 `{"error": ...}`。

## 输入模型

| 对象 | 关键字段 | 说明 |
| --- | --- | --- |
| `GatewayConfig` | `trusted_forwarders` | 受信任转发服务标识集合 |
| | `domain_policies` | 发件域策略列表（精确域优先，组织域回退） |
| | `org_registry` | 组织域注册表，如 `{"co.uk": "example.co.uk"}` |
| `DomainPolicy` | `domain` / `policy` | 策略域与动作：`none` / `quarantine` / `reject` |
| | `spf_alignment` / `dkim_alignment` | `strict`（完全一致）或 `relaxed`（同组织域） |
| | `subdomain_policy` | 子域回退动作；缺省沿用主策略 |
| `Message` | `message_id` / `sender_domain` | 邮件标识与发件域 |
| | `local_evidence` / `upstream_evidence` | 本地与上游认证证据（`AuthEvidence`） |
| | `forward_hops` | 转发跳列表；为空表示直达邮件 |
| `AuthEvidence` | `method` / `result` / `auth_domain` | 认证方式、结果、认证域 |
| | `source` | `local` / `upstream`，构造时强校验归属 |
| | `message_id` | 上游证据绑定的邮件标识 |
| `ForwardHop` | `service` / `trusted` | 转发服务标识与该跳上报的信任状态 |

所有模型为不可变 dataclass，构造时完成归一化（小写、去尾点）与防御性
校验：非法域名、未知枚举值、空白 `message_id`、证据来源与所在列表不匹配
等都会抛出 `ValueError`。

## 裁决流程

1. **输入类型校验**：`evaluate()` 拒绝非 `Message` / `GatewayConfig` 入参。
2. **信任链评估**（`engine._evaluate_trust_chain`）：
   - **直达邮件**（`forward_hops` 为空）：只看本地证据；即使携带上游
     证据也显式忽略（原因码 `upstream_ignored_trust_chain_broken`）。
   - **转发邮件**：逐跳校验，一跳可信当且仅当
     `hop.trusted is True` **且** `hop.service ∈ trusted_forwarders`，
     两个条件缺一不可。任一跳失败即整条链中断，原因码中逐跳点名。
   - 链中断时**丢弃全部上游结果**，仅保留本地证据继续裁决——本地仍
     有效的对齐证明不会因链中断而失效。
   - 链完整时，只采用 `message_id` 与本邮件完全相等的上游证据；绑定
     其他邮件 ID 的证据被排除（`upstream_message_id_mismatch`）；没有
     任何绑定证据时回退本地证据（`upstream_no_bound_evidence`）。
3. **策略查找**：精确域名优先，其次按组织域（`org_registry`，无登记时
   退化取末两段标签）匹配。无策略覆盖时失败闭合，直接隔离。
4. **对齐与认证检查**：按策略为每类证据选择对齐模式（DMARC 聚合证据
   在 SPF/DKIM 任一要求严格时按严格处理），逐条检查；只有
   `result == pass` **且** 域对齐的证据才算成功证据。
   `none` / `neutral` / `softfail` / 临时或永久错误 / 证据缺失一律不是成功。
5. **处置映射**：
   - 存在对齐 pass：`deliver`，`dmarc_verdict="pass"`，`evidence_used`
     指向命中的那条证据；
   - 否则 `dmarc_verdict="fail"`、`evidence_used=null`，按策略动作映射：
     `reject → reject`、`quarantine → quarantine`、`none → quarantine`
     （监控策略下失败同样不投递）、无策略 → 隔离。

## 输出 Decision 字段

```json
{
  "message_id": "msg-fwd-002",
  "disposition": "deliver",
  "dmarc_verdict": "pass",
  "evidence_used": { "...": "命中的那条证据，失败时为 null" },
  "policy_domain": "example.com",
  "policy_action": "reject",
  "reasons": ["upstream_result_adopted", "dmarc_aligned_pass"],
  "evaluated_evidence": ["实际参与裁决的候选证据全集"],
  "trust_chain_valid": true
}
```

原因码（`mail_security/engine.py` 常量）：

| 原因码 | 含义 |
| --- | --- |
| `direct_mail_local_only` | 直达邮件，仅依据本地证据裁决 |
| `upstream_result_adopted` | 信任链完整且 ID 绑定，采用上游结果 |
| `upstream_message_id_mismatch` | 存在绑定其他邮件 ID 的上游证据，已排除 |
| `upstream_no_bound_evidence` | 链可信但无绑定本邮件的上游证据，回退本地 |
| `upstream_ignored_trust_chain_broken` | 直达邮件或链中断，上游证据被忽略 |
| `trust_chain_broken` / `trust_chain_broken_hop:<service>` | 链中断及中断跳 |
| `no_domain_policy` | 无策略覆盖，失败闭合隔离 |
| `dmarc_aligned_pass` | 找到对齐且显式 pass 的证据 |
| `no_evidence_available` | 候选证据为空——缺失证明未被当作成功 |
| `no_aligned_pass` | 有证据但无一对齐 pass |
| `policy_enforced:<action>` | 最终执行的策略动作 |

## 验证范围与测试

运行方式（在工作区根目录）：

```bash
python -m unittest discover -s tests -v
```

共 32 个用例，全部为真实断言（无空测试），按题面交叉约束分四组：

- **可接受结果（应放行）**：直达本地 DKIM pass；可信转发链采用绑定同
  一 ID 的上游 pass；宽松对齐子域放行；组织域注册表多标签后缀。
- **应阻止/拒绝的结果**：无证据隔离；`none` 等非 pass 结果拒收/隔离；
  显式 fail 拒收；`p=none` 失败降级隔离；链中断丢弃上游（本地证明保留
  有效 / 本地无证明则拒收两条分支）；上报不可信跳断链；ID 绑定到其他
  邮件不采用；无绑定上游证据回退；直达邮件忽略上游；严格/宽松不对齐
  均不放行。
- **边界与交错场景**：精确域与子域策略边界；组织域末两段退化；混合
  fail/none/pass 证据中 pass 仍可命中；可信/不可信跳交错致整链中断；
  未知发件域失败闭合；空转发跳即直达；DMARC 组合对齐模式；JSON 往返
  裁决一致性。
- **防御性校验**：非法域名、未知枚举、证据来源错配、空白邮件 ID、
  错误入参类型全部抛出 `ValueError`。

`tests/test_cli.py` 另以真实子进程执行 `python -m mail_security`，
断言 payload 模式、分文件模式的 JSON 输出与错误输入的非零退出码。

## 设计边界与说明

- 模块只做**防御性检测与本地裁决**：不发起任何网络请求、不探测真实
  目标，全部夹具为本地合成数据。
- 组织域缺省退化规则（末两段标签）不覆盖 `co.uk` 类公共后缀，生产
  部署应以完整公共后缀表通过 `org_registry` 注入；注册表取最长后缀
  匹配。
- 信任链完整时以上游绑定证据为裁决候选，本地证据不再参与；该选择
  对应“转发时采用上游验真结果”的题面语义，链一旦中断即回到本地证据。
