# 发送前密钥覆盖检查（key coverage check）

邮件客户端在发送加密通知前，需要确认目标通讯组里**每一位有资格的收件人**在
**预定投递时刻**都持有有效加密密钥。本包只用 Python 3.11 标准库实现该检查，
输入是按版本保存的目录快照（组成员、别名、人员及其密钥有效时段），输出是可直接
驱动“发送 / 停在准备状态”的检查结论。

## 规则与语义

1. **目录版本固定**：一封邮件固定一个目录版本（`directory_version`），遍历全程
   只看该快照，不与其他版本混读。
2. **嵌套组与别名**：组成员可以是人员、子组或别名；别名可指向人员、组或另一个
   别名。别名自身不携带密级约束，约束只挂在组成员边上。
3. **按路径的密级约束**：每条成员边可带 `min_level`。密级固定顺序为
   `public < internal < confidential < secret < top_secret`。一条到达路径上的
   所有边约束都满足时，该路径才算合格。
4. **多路径资格合并（并集）**：同一人员沿不同路径到达时，只要有一条合格路径即
   有资格；被阻断的路径不否定其他合格路径。全部路径都被阻断的人员进入
   `restricted`，不算有资格，因此也不会因缺密钥而停发。
5. **环（自引用 / 互引用）**：按“当前遍历栈内已访问”截断环并在 `cycle_cuts`
   留痕；截断前沿其他分支已经到达的真实成员不会丢失。
6. **密钥有效时段**：按半开区间 `[valid_from, valid_to)` 判定，`None` 表示该
   方向无限。投递时刻等于 `valid_to` 时密钥已失效。
7. **停发条件**：任一有资格人员在投递时刻没有有效密钥（或目录存在悬空引用等
   结构性错误），整封邮件为 `preparing`；否则为 `ready`。
8. **原因链**：缺密钥失败给出从原始组展示名/ID 出发、逐跳到该人员的可读原因链
   （`MissingKeyFailure.reason_chain`），并附结构化路径与每把密钥的失效原因。
9. **按密钥去重**：最终收件人按实际选中的加密密钥 `key_id` 去重；同一人员经多
   条路径到达只产生一条记录，其全部合格路径保留在明细中。
10. **选钥确定性**：一人多把有效密钥时，优先选 `valid_to` 最晚者（无结束时间
    最优），并列时取 `key_id` 字典序最小者，保证结果可复现。

## 目录结构

```
key_coverage/
  __init__.py        # 公开 API 再导出
  model.py           # 数据模型与枚举（组、别名、人员、密钥、快照、结果）
  check.py           # check_key_coverage 核心算法
tests/
  builders.py        # 合成数据构造辅助（不参与测试发现）
  test_normal_coverage.py
  test_paths_and_cycles.py
  test_failures_and_interleaving.py
smoke_demo.py        # 端到端冒烟演示
```

## 公开 API

主入口：

```python
from key_coverage import check_key_coverage

result = check_key_coverage(
    store,
    directory_version="v1",
    root_group_id="g-root",
    notification_level="internal",
    scheduled_at=datetime(2026, 11, 28, 12, tzinfo=timezone.utc),
)
```

| 名称 | 说明 |
| --- | --- |
| `check_key_coverage(store, *, directory_version, root_group_id, notification_level, scheduled_at)` | 执行检查，返回 `CoverageResult`。版本/根组不存在抛 `KeyError`；未知密级、无时区时间抛 `ValueError`。 |
| `DirectoryStore` / `DirectorySnapshot` | 按版本保存的目录与单个不可变快照。 |
| `Group` / `MemberRef` / `MemberKind` | 通讯组、成员引用（含 `min_level`）与成员类型枚举（person/group/alias）。 |
| `Alias` | 别名，指向人员/组/另一别名。 |
| `Person` / `KeyRecord` | 人员与其密钥；`KeyRecord.is_valid_at(when)` 做半开区间判定。 |
| `Status` | `READY` / `PREPARING`。 |
| `CoverageResult` | `status`、`recipients`、`missing_key_failures`、`reached_person_ids`、`restricted`、`cycle_cuts`、`structural_errors` 等。 |
| `RecipientCoverage` | 去重后的最终收件人：人员、选中的 `key_id`、合格路径。 |
| `MissingKeyFailure` | 缺密钥人员、合格路径、逐密钥诊断 `KeyDiagnostic`、可读原因链。 |
| `PathNode` | 路径上的一跳（ID、类型、展示名、该边密级要求）。 |

## 运行方法

```bash
# 运行全部单元测试（题目指定方式）
python3 -m unittest discover -s tests -v

# 端到端冒烟演示（隔离模式，无网络、无第三方依赖）
python3 -I smoke_demo.py
```

## 验证范围（本地合成数据）

共 27 个单元测试，全部通过，覆盖：

- **正常场景**：嵌套组展开到全部人员；开放/半开放有效时段；空组返回 ready。
- **边界**：半开区间左右端点（含投递时刻恰为 `valid_to`）；密级同级满足、低一
  级阻断；无限早/无限晚窗口。
- **版本固定**：固定旧版本时看不到新版本才加入的成员。
- **去重与选钥**：同一人员多路径到达按密钥去重；最晚到期优先、并列按 key_id。
- **资格合并（交错）**：一人一条受限路径 + 一条合格路径时仍有资格；全受限人员
  即使没密钥也不停发。
- **环**：自引用组真实成员不丢；互引用组终止且全员覆盖；环分支不吞并列成员；
  别名环被截断。
- **失败与原因链**：无密钥 / 全过期 / 未生效密钥导致 preparing；原因链含原始组
  名、逐跳路径与每把密钥的失效原因；一人缺钥即整封停发，其余人选钥结果保留。
- **结构性 / 输入校验**：悬空组引用记为结构错误并停发；未知版本、未知根组抛
  `KeyError`；朴素时间、未知密级抛 `ValueError`。
- **复合交错**：自引用环 + secret 边 + 过期密钥同时出现时，环截断、资格并集与
  停发原因链同时成立。

## 设计取舍（题面未精确处的明确约定）

- “只接收特定密级的通知”建模为成员边上的 `min_level`：通知密级达到要求才可经
  该边到达人员；别名边不带约束。
- 有效时段采用半开区间，右端点失效，避免“到期瞬间仍可发信”的歧义。
- 去重粒度为实际选中的 `key_id`；数据正常时一人的密钥 ID 唯一，因此等价于按人
  去重，同时也防御跨记录的重复密钥。
- 悬空引用不静默跳过：记入 `structural_errors` 并使邮件停在准备状态。
- 全部输入时间要求带时区，防止跨时区比较错误。
