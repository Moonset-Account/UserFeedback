# 账户关联模块（机构合并场景）

两家机构合并时，员工可能用不同的外部身份登录同一个应用。本模块接收双方
**经过验证且带有效期**的所有权证明，由用户指定保留账户，完成账户归并，
并在之后通过任一原身份登录时解析到同一保留账户、保有来源记录。

## 实际入口

- 包：`account_linking/`
  - `account_linking/service.py` — `AccountLinkingService`（核心服务，线程安全）、`OwnershipProof`
  - `account_linking/models.py` — `Account` / `ExternalIdentity` / `LinkSession` / `LinkRecord` / `SessionStatus`
  - `account_linking/errors.py` — 业务异常（证明过期/无效、身份已被占用、会话已终态等）
- 测试：`tests/test_account_linking.py`（unittest，16 个用例）

### 运行方式（Python 3.11.6）

```bash
.venv/bin/python -m unittest discover -s tests -v
```

### 典型调用流程

```python
from datetime import datetime, timedelta
from account_linking import AccountLinkingService, OwnershipProof

svc = AccountLinkingService()
svc.register_account("acc_a", "employee@example.com")
svc.register_account("acc_b", "employee@example.com")
svc.register_identity("id_a", "idp-alpha", "sub-a", "acc_a")
svc.register_identity("id_b", "idp-beta",  "sub-b", "acc_b")

svc.suggest_candidates("employee@example.com")   # 仅提示候选，不自动关联

session = svc.start_link_session("acc_a", "acc_b", keep_account_id="acc_a")
now = datetime.now()
proof = lambda iid: OwnershipProof(iid, verified=True,
                                   expires_at=now + timedelta(hours=1))
svc.submit_proof(session.session_id, proof("id_a"))   # 双方分别证明
svc.submit_proof(session.session_id, proof("id_b"))
svc.confirm(session.session_id)

svc.resolve_login("id_b")   # -> ("acc_a", [LinkRecord(...)]) 含来源记录
```

## 验证范围（题面交叉约束 → 测试用例）

| 约束 | 用例 |
| --- | --- |
| 可接受结果：双方证明齐备 + 指定保留账户，确认后两个原身份解析到同一保留账户且有来源记录 | `AcceptableFlowTest` |
| 邮箱相同只能提示候选，不产生任何关联 | `CandidateHintTest` |
| 外部身份已关联到其他保留账户 → 阻止（会话开启时 + 确认时防御性复查） | `BlockedFlowTest.test_identity_already_linked_*` / `test_confirm_time_guard_*` |
| 证明过期 / 未验证 / 来自无关身份 → 拒绝；提交后确认前过期 → 拒绝 | `BlockedFlowTest.test_expired/unverified/outsider/proof_expiring_before_confirm` |
| 保留账户必须是双方之一；双方证明未齐备不得确认 | `BlockedFlowTest.test_keep_account_must_be_one_of_the_two` / `test_confirm_requires_both_proofs` |
| 确认与任一方撤回并发（Barrier 交错 20 轮）→ 恰好一种最终结果，后到者被 `SessionFinalizedError` 拒绝 | `ConcurrentFinalizationTest.test_concurrent_confirm_and_withdraw_single_outcome` |
| 终态之后的确认/撤回均被拒绝；撤回不落账、确认不回滚 | `ConcurrentFinalizationTest.test_confirm_after_withdraw` / `test_withdraw_after_confirm` |
| 未知身份登录解析 → `KeyError` | `ResolveLoginTest` |

## 设计要点

- **原子终态**：`confirm` 与 `withdraw` 在同一把锁内完成"检查—决策—落账"，
  并发到达时先到者生效、后到者收到 `SessionFinalizedError`，只产生一种最终结果。
- **证明双重校验**：提交时校验 `verified` 与有效期，确认时刻再次校验有效期，
  防止"提交时有效、确认时已过期"的窗口。
- **占用检查**：确认时复查待迁移身份是否已被其他保留账户占用，命中即阻止且会话不落终态。
- **来源记录**：每次合并写入 `LinkRecord(identity, source_account, retained_account, linked_at)`，
  `resolve_login` 沿 `merged_into` 链解析到保留账户并返回全部来源记录。

## 范围说明

内存实现、本地合成夹具，仅用于本工作区交付与核验；不涉及真实目标探测、
外部服务或网络访问。生产化时需将内存存储替换为带唯一约束与事务的持久层
（锁语义对应数据库行锁/唯一索引）。
