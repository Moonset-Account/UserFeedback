# 专网隧道提案协商模块

双方各自提交**有序提案**，每份提案自包含可搭配的加密算法、完整性算法与
密钥交换组清单，模块在网关强度政策约束下协商出唯一套件，并能区分
「政策淘汰」与「双方没有共同提案」两类失败。

## 目录结构

```
tunnel_negotiation/
├── __init__.py      # 公共 API 导出
├── errors.py        # NegotiationError 及三类具体失败
├── models.py        # Proposal / SelectedSuite / RejectedSuite / PairDiagnostic
├── policy.py        # Policy 强度注册表、AEAD 集合、default_policy()
└── negotiation.py   # negotiate() / negotiate_rekey() / validate_proposal()
tests/
└── test_negotiation.py
```

## 实际入口（无第三方依赖，Python 3.11）

测试入口（在工作区根目录执行，二者均执行真实 `assertEqual/assertRaises` 断言）：

```bash
.venv/bin/python -m unittest discover -s tests -v   # 入口 1：unittest 发现
.venv/bin/python tests/test_negotiation.py          # 入口 2：直接脚本运行
```

库入口：

```python
from tunnel_negotiation import (
    Proposal, default_policy, negotiate, negotiate_rekey,
    PolicyRejectionError, NoCommonProposalError,
)

local  = [Proposal(1, ["AES256-CBC"], ["HMAC-SHA384-192"], ["DH19", "DH20"])]
remote = [Proposal(2, ["AES256-CBC"], ["HMAC-SHA384-192"], ["DH20", "DH19"])]
suite  = negotiate(local, remote, default_policy(min_strength=128))

# 换钥：生效门槛 = max(政策门槛, 上一代连接最低等级)
rekeyed = negotiate_rekey(local, remote, default_policy(), prior_suite_strength=192)
```

## 协商规则（对应题面约束）

1. **完整提案配对**：套件的加密、完整性、密钥交换组必须共同来自同一份
   本端提案和同一份对端提案。某组件只在另一份提案出现时**禁止拼接**，
   匹配只在提案对内部进行。
2. **AEAD 约束**：内置认证的加密算法（`AES128-GCM`、`AES256-GCM`）
   不能另配完整性算法——AEAD 提案的完整性清单必须为空，构造/校验阶段
   即拒绝非法提案，协商层还有配对级二次拦截；非 AEAD 加密必须搭配一个
   双方共有的完整性算法。同一份提案禁止混用 AEAD 与非 AEAD 加密。
3. **强度等级**：政策注册表给出每个算法/分组的强度，套件强度取所有
   组件的最小值；低于 `Policy.min_strength` 的候选被政策淘汰并留档。
4. **换钥**：`negotiate_rekey` 叠加「上一代连接最低等级」，生效门槛为
   `max(policy.min_strength, prior_suite_strength)`，防止降级。
5. **唯一结果的选择顺序**：先按本端提案顺序，再按对端提案顺序；
   同一对提案内按本端组内偏好枚举（加密 → 完整性 → 密钥交换组，
   各取本端清单顺序），第一个达标的候选即最终结果，对端清单顺序不参与
   组内偏好。
6. **失败区分**：
   - `NoCommonProposalError` —— 结构性失败，任何一对提案都组不成完整
     共同套件（算法/分组不相交、AEAD 另配完整性、跨提案不可拼接等），
     携带逐对提案的 `PairDiagnostic`；
   - `PolicyRejectionError` —— 结构性共同套件存在，但全部未达强度
     门槛，携带每个被淘汰候选的 `RejectedSuite` 与生效门槛；
   - `UnknownAlgorithmError` —— 提案引用了政策未登记的算法/分组。

## 测试验证范围（23 个用例，全部通过）

| 分组 | 覆盖内容 |
| --- | --- |
| 可接受结果 | AEAD 套件（无独立完整性）、非 AEAD 套件完整性选择、两类算法分提案共存 |
| 应被阻止/拒绝 | AEAD 另配完整性、单提案混用 AEAD、非 AEAD 缺完整性、未知算法、政策淘汰、无共同提案，并断言两类失败互不混淆 |
| 边界 | 强度恰等于门槛接受、仅低一级被拒 |
| 交错场景 | 跨提案不可拼接（四对提案逐一诊断）、本端提案顺序压过强度、对端提案顺序打破同本端提案平局、组内本端偏好、先淘汰后成功继续搜索、全部淘汰时候选全留档 |
| 换钥 | 上代等级抬升门槛、不可达门槛走政策淘汰、政策门槛高于上代等级时取高者 |
| 构造校验 | 清单重复算法、空加密/空分组、重复提案 id |
