# 分批收敛的命名空间终结控制器

命名空间收到删除请求后，本控制器以**分批收敛**的方式完成终结：发现资源类型 →
按类型分页列出对象（清单固定到所选控制面修订号）→ 每轮限量发出删除动作并按
对象 UID 追踪回执 → 在同一轮发现与清单屏障下确认没有遗留对象后才允许终结。

- 运行环境：Python 3.11.6（仅标准库，无第三方依赖）
- 代码：`nsfinalizer/`（实现）、`tests/`（unittest 测试）

## 公开 API

### `NamespaceFinalizerController(namespace_uid, revision=0, max_deletes_per_round=50)`

| 成员 | 说明 |
| --- | --- |
| `observe_discovery(revision, types) -> bool` | 接受发现结果。修订号低于当前所选修订号则忽略；前进时所有清单证据过期；新增类型需重新获得空集证据；既有类型在无失效事件时保留确认。 |
| `advance_revision(revision) -> bool` | 显式推进所选修订号（不改动发现集），之后需重新观察发现以重建屏障。 |
| `observe_page(type, revision, page_token, items, next_page_token) -> bool` | 接受一页清单。页必须固定到当前修订号且 token 与分页进度衔接（第一页 token 为 `""`，末页 `next_page_token` 为 `None`/`""`），否则拒绝。 |
| `observe_update(type, uid, name, revision=None) -> bool` | 对象更新事件。新对象使该类型空集证据失效（需重新列举）；同名重建带新 UID 按新对象处理；墓碑对象与落后于屏障的事件被忽略。 |
| `observe_delete_receipt(uid) -> bool` | 按对象 UID 匹配删除回执；未知/重复 UID 返回 `False`。 |
| `step() -> list[DeleteAction]` | 执行一轮：对已知存活且未决的对象发出删除动作，每轮最多 `max_deletes_per_round` 个，按 (类型, 名称, UID) 确定性排序，未决对象不重复发出。 |
| `pending_deletes() -> tuple[DeleteAction, ...]` | 当前所有未决删除动作。 |
| `blocking() -> BlockingReport` | 返回阻止终结的类型与具体对象，原因包括 `awaiting_discovery` / `listing_incomplete` / `listing_stale` / `objects_remaining` / `deletes_pending`。 |
| `finalized() -> bool` | 是否允许终结：同一轮发现与清单屏障下，所有被发现类型持有当前修订号的完整清单、无遗留对象、无未决回执。 |
| `archive() -> dict` / `NamespaceFinalizerController.restore(data)` | 状态序列化为纯 JSON 结构 / 恢复。恢复后未决动作继续按 UID 等待回执。 |

数据结构：`ObjectRef(uid, name)`、`DeleteAction(type, uid, name)`、
`BlockingType(type, reason, objects)`、`BlockingReport(namespace_uid, revision,
discovery_observed, types)`（含 `is_blocked`、`type_names()`）。

## 关键语义

1. **修订号屏障**：发现与对象状态共享一个可指定的控制面修订号；分页清单必须
   逐页固定到该修订号，修订号不符或 token 断链的页被拒绝。
2. **空集证据失效**：修订号前进（清单版本过期）、发现新增类型、新对象更新事件，
   都会要求重新获得足以覆盖该类型的空集证据；无失效事件的已收敛类型保留确认。
3. **UID 追踪**：删除回执严格按 UID 匹配；对象被删后同名重建得到新 UID，作为
   新对象重新处理；已删对象的滞后事件被墓碑过滤。
4. **终结条件**：即使每种类型都曾各自返回空集，只要不在同一轮发现+清单屏障下，
   就不得终结。

## 复现步骤

```bash
cd <工作区根目录>
python3 --version            # 应为 3.11.6
python3 -m unittest discover -v
```

## 验证范围（29 个用例，全部真实执行通过）

- **正常流程**：空命名空间直接终结；单页/多页清单 → 分批删除 → 回执 → 终结；
  删除动作排序确定性。
- **边界**：每轮删除上限（5 对象 / 上限 2 → 2,2,1,0 且未决不重发）；上限为 0；
  页修订号过旧/超前被拒；页 token 断链/乱序被拒；未知与重复回执；过期发现
  与过期更新事件被忽略；`advance_revision` 不前进则拒绝；坏存档拒绝恢复。
- **失效与重建**：修订号前进使确认过期（`listing_stale`）；同修订号新增类型只
  要求新类型补证据、既有确认保留；更新事件使确认失效；同名重建重新处理；
  墓碑过滤已删 UID 的滞后更新；未知类型先阻塞（`awaiting_discovery`）后被
  发现覆盖。
- **屏障语义**：两种类型各自曾在不同修订号返回空集仍不得终结，必须在同一轮
  屏障下补齐；逐轮删除时最后一笔回执到达前不得终结。
- **交错场景**：分页、更新、回执、多轮 `step()` 交错推进；列举进行中收到新对象
  更新则分页作废重来；分页进行中修订号前进则旧页被拒。
- **存档恢复**：存档 JSON 往返；恢复后未决动作继续按 UID 匹配回执、剩余对象
  继续分批删除直至终结；恢复后 `blocking()` 报告与存档前一致，且包含具体
  类型与对象。

未执行项：无（以上用例均已实际运行）。
