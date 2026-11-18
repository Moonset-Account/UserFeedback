# scanscope — 防护扫描范围模块

## 实际入口

- 模块路径：`scanscope`（Go 1.20.2，见 `go.mod`）
- 核心 API：`func Evaluate(req *Request) (*Report, error)`，位于 `scope.go`
- 输入 `Request`：
  - `Snapshot *SnapshotNode`：目录快照树；每个节点带稳定对象标识 `ObjectID`（对象移动/重命名后保持不变，新目录对象拥有新 ID）
  - `Exclusions []Exclusion`：限时排除声明，含对象 ID、**批准路径** `ApprovedPath` 与到期时间 `ExpiresAt`
  - `ProtectedRoots []string`：受保护根（绝对路径，按路径组件匹配）
  - `RenameRecords` / `SyncRecords`：重命名与同步证据
  - `Now time.Time`：评估时刻
- 输出 `Report`：
  - `ScanPaths`：应扫描的全部节点（字典序）
  - `ExcludedPaths`：排除仍然生效的锚点目录（其子树整体跳过）
  - `Restorations`：触发恢复扫描的事件，含路径、对象、排除声明 ID 与原因

恢复原因（`Reason`）：

| 原因 | 含义 |
| --- | --- |
| `protected_root` | 节点位于受保护根内（含嵌套受保护根），始终恢复扫描，覆盖一切排除 |
| `exclusion_expired` | 排除已过有效期（到期时刻即失效，严格 `Now < ExpiresAt` 才有效） |
| `moved_without_record` | 原对象被移动/重命名，且无匹配的重命名或同步记录，仅受影响分支丧失排除资格 |
| `path_reused_by_new_object` | 批准路径被另一个对象占用且无源流证据，新对象按新对象评估，不能继承旧排除 |

## 核心规则

1. **排除必须同时匹配原目录对象与批准路径**，并在时间窗口内；不做通配或前缀匹配。
2. **嵌套的受保护根始终恢复扫描**：即使受保护根位于已排除子树内部，进入该根及其下全部节点都恢复扫描，并记录 `protected_root`。
3. **搬移后的排除不得进入受保护区域**：即便存在重命名记录，迁入受保护根的旧排除也被受保护根覆盖。
4. **无重命名/同步记录时只取消受影响分支**：未记录的搬移使源路径（路径复用）与目标路径（对象迁移）两个分支恢复扫描，其他目录上的独立排除继续生效。
5. **有重命名/同步记录时排除可跟随对象**迁移到新路径；路径上的新对象因无继承资格仍被扫描。
6. **路径复用按新对象评估**：没有源流证据连接时，占用批准路径的新对象不会继承旧排除。
7. 同一节点存在多条声明时，任一有效声明即可排除；失效声明在有效声明存在时不产生恢复事件。
8. 输入校验：拒绝空请求/快照、零评估时刻、相对或空的受保护根、缺少对象 ID/批准路径/到期时间的排除、快照内重复对象 ID。

## 验证范围

测试入口 `scope_test.go` 全部使用 Go 内置 `testing`，对 `Evaluate` 的真实返回做断言（集合比对 + 恢复原因断言 + 错误断言），无占位测试。

| 测试 | 约束类型 | 覆盖内容 |
| --- | --- | --- |
| `TestValidExclusionIsHonored` | 可接受结果 | 有效限时排除生效，锚点子树整体跳过，受保护根正常扫描 |
| `TestMovedWithoutRecordRevokesOnlyAffectedBranch` | 应被阻止 | 无记录搬移：源路径 `path_reused_by_new_object`、目标路径 `moved_without_record` 均恢复；无关分支的排除不受影响 |
| `TestRenameRecordLetsExclusionFollowObject` | 交错场景 | 有重命名记录时排除跟随对象；旧路径的新对象仍被扫描 |
| `TestMovedExclusionCannotEnterProtectedRoot` | 应被阻止 | 记录在案的搬移目标处于受保护根内，旧排除被拦截，记录 `protected_root` |
| `TestNestedProtectedRootResumesScanning` | 应被阻止 | 排除子树内部的嵌套受保护根恢复整支扫描 |
| `TestExpiryBoundary` | 边界 | 到期前 1 纳秒仍排除；到达到期时刻立即失效并恢复 |
| `TestProtectedRootComponentBoundary` | 边界 | 受保护根按路径组件匹配，`/protect` 不误伤 `/protected-x` |
| `TestSyncRecordProvenance` | 交错场景 | 同步记录作为源流证据，排除可延伸到镜像路径 |
| `TestMultipleDeclarationsValidWinsOverExpired` | 交错场景 | 有效声明与过期声明并存时以有效声明为准 |
| `TestValidationRejectsMalformedInput` | 输入校验 | 9 个畸形输入子用例均返回错误 |

## 本地验证

```sh
go test ./... -v
```

已在本地 Go 1.20.2 (darwin/arm64) 通过全部用例。另尝试执行 `go vet ./...`，因当前环境未授予该命令审批而未运行；编译与全部测试均由 `go test ./...` 实际执行通过。

## 设计边界说明

- 快照为内存中的合成树结构，模块不访问真实文件系统，也不执行任何目标探测或扫描动作；它只计算“应扫描/应排除”的范围与恢复原因。
- 排除锚点内部若出现嵌套受保护根，恢复事件只在受保护根边界记录一次，根内其余节点正常列入扫描范围。
