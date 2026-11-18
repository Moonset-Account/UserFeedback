# authz_audit — 并发撤权离线审计库

核查授权服务在并发撤权/权限变更期间是否错误返回了受保护数据。
输入同一租户的初始权限、资源字段分级与合成操作记录,离线判断
已完成的读取操作能否排成符合授权规则的顺序。

## 规则模型

- **字段分级**:`FieldGrading` 给出每个资源的字段级别;
  `expand_level_grant` 把"可见到某一级"展开为显式字段集合。
- **组权限变更**(`PermissionChange`):一次变更对组内**所有成员同时**
  生效;生效时刻落在 `window` 闭区间内;`version` 为授权版本,
  同租户内变更必须按版本非递减生效;`receipt=False` 表示缺失生效回执。
- **读取记录**(`ReadRecord`):带调用起止时刻 `[start, end]`、操作对象
  (主体 + 资源)、可选的授权版本 `authz_version`、实际返回的字段集合。
- **核心约束**:一次读取返回的字段,必须能由调用时段内**某一个时刻**的
  完整授权状态解释 —— 不允许把两代授权各允许的字段拼接到一起。
  时刻语义为闭区间:读取若恰好落在变更生效瞬间,可观察到变更前或后的
  任一代(但不能跨代拼接)。

## 判定结论(`Verdict`)

| 结论 | 含义 |
| --- | --- |
| `CONSISTENT` | 存在合法调度;`change_times`/`read_witnesses` 给出判定依据(每个变更的生效时刻、每条读取的观察时刻与状态版本)。 |
| `CONFLICT` | 所有顺序均矛盾;`conflicting_reads` 给出贪心删除法求得的最小冲突记录集合,`involved_changes` 列出相关变更。 |
| `MANUAL_REVIEW` | 影响结论的回执缺失:`pending_receipts` 列出缺失回执且结论依赖其确切生效时刻的变更,转人工复核。回执缺失但不影响结论时照常判定。 |

## 实际入口

```python
from authz_audit import (
    AuditInput, Auditor, FieldGrading, PermissionChange,
    ReadRecord, Subject, Verdict, expand_level_grant,
)

result = Auditor(audit_input).run()
# result.verdict / result.change_times / result.read_witnesses
# result.conflicting_reads / result.involved_changes / result.pending_receipts
```

- 库代码:`authz_audit/models.py`(数据模型)、`authz_audit/checker.py`(判定核心)。
- 测试入口:`python3 -m unittest discover -s tests -v`(Python 3.11.6,仅标准库)。

## 验证范围(`tests/test_audit.py`,11 个用例,均含真实断言)

- **可接受结果**:时段重叠但存在合法顺序(含版本声明、组变更对两名成员
  在同一边界时刻生效、读取恰落在生效瞬间)。
- **应被拒绝的结果**:返回字段横跨两代授权(拼接)、声明版本与时段矛盾、
  返回任何一代都未授权的字段。
- **边界/交错场景**:好坏记录交错时的最小冲突定位;多次变更的版本顺序
  约束(合法与矛盾各一);回执缺失且影响结论(一致与冲突两种情形)转人工,
  回执缺失但不影响结论时照常判 CONSISTENT。

## 实现说明

- 判定通过枚举版本非递减的变更顺序,再在窗口端点候选集上深度优先搜索
  非递减的生效时刻赋值(约束均为上下界形式,存在可行解则存在取端点的解),
  最后对每条读取在变更序列的槽位中寻找可行观察时刻。
- 搜索规模设有安全上限(`_MAX_ASSIGNMENTS`),面向合成记录规模设计;
  全部产物留在本地,不进行任何网络访问或真实目标探测。
