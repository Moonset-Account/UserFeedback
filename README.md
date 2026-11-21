# 节点检修驱逐选择器（eviction）

集群控制器组件：工作节点检修时，从候选中就绪 Pod 里挑出**本轮可以同时驱逐的最大可行集合**。

## 实际入口

- `eviction.go`
  - `NewController() *Controller` — 创建控制器，内部记录已批准的驱逐。
  - `(*Controller).Admit(s Snapshot, candidates []string) []string` — 本轮准入口径：选出最大可行集、登记批准、返回按名字排序的放行名单。重复调用会看到预算已被消耗。
  - `Select(s Snapshot, approved, candidates)` — 纯函数口径的选择逻辑（不登记）。
- 核心规则：
  - 每条 `Budget` 用 `Selector` 标签选 Pod，要求整个集群至少保留 `MinAvailable` 个就绪副本。
  - 已获准驱逐但在快照中仍就绪的副本先从就绪数中扣掉；一旦变为未就绪，它本就不计入就绪数，**合计只扣一次**（`effectiveReady`）。
  - 一轮至多 `MaxCandidates = 16` 个候选（去重、剔除未就绪/不存在者、按名字排序后截断）。
  - 枚举子集取**数量最多**的可行集合；同数时取**排序后名字序列字典序最小**的一组。

## 验证范围（`eviction_test.go`，Go 内置 testing）

运行方式：

```sh
go test ./...
```

| 测试 | 覆盖的交叉约束 |
| --- | --- |
| `TestAdmitPrefersTwoSingleBudgetPods` | 题面场景（可接受结果）：两条预算各剩一个名额，一个候选同占两边、另两个各占一边 → 放行后两个 |
| `TestNoSlotsLeftRejectsAll` | 应被阻止：就绪数已等于 MinAvailable，任何驱逐都违规 → 空集 |
| `TestBoundaryExactSlotAndTieBreak` | 边界：就绪数 = MinAvailable+1 恰好放行一个、不能放两个；同数取字典序最小 |
| `TestSameSnapshotTwiceDoesNotDoubleSpend` | 交错：同一快照连续申请两次，第二次不得重复花同一份预算 |
| `TestApprovedReplicaDeductedOnceAcrossReadyTransition` | 交错边界：已批准副本从就绪变未就绪后只扣一次，剩余名额不增不减 |
| `TestCandidateCapAtSixteen` | 边界：17 个候选只取字典序前 16 个 |
| `TestUnreadyAndUnknownCandidatesDropped` | 未就绪/不存在/重复的候选被剔除 |

所有测试入口均执行真实断言（`reflect.DeepEqual` / 长度检查），无空转用例。
