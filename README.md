# dictpool — 压缩字典池（公共/私有分支隔离）

一个纯 Go（Go 1.20.2，仅依赖标准库）实现的压缩字典池。公共帮助文本通过
公共快照在不同客户间共享；写入客户私有数据时先做写时复制（copy-on-write），
把可写状态与公共分支分离；派生副本始终继承原分支的保密范围。

## 入口与结构

- `go.mod`：模块 `dictpool`，`go 1.20`。
- `dictpool.go`：全部实现，包名 `dictpool`。
  - 类型：`Pool`、`Snapshot`、`Handle`、`Entry`、`SourceLabel`、`Scope`。
  - 生命周期入口：
    - `New()` 创建字典池。
    - `(*Pool).CreatePublicSnapshot(texts)` 从公共帮助文本创建不可变公共快照。
    - `(*Pool).Derive(customer, snapshotID)` 从快照派生可写句柄；私有快照只允许
      属主客户派生，跨客户返回 `ErrNotAuthorized`。
    - `(*Pool).Append(handleID, customer, text)` 追加客户私有字典文本；首次追加
      触发写时复制并把句柄提升为该客户的私有范围。
    - `(*Pool).SaveSnapshot(handleID)` 把当前句柄状态存为新的不可变快照。
    - `(*Pool).Return(handleID)` 归还句柄；归还后一切句柄操作被拒绝，已提交快照
      不受影响。
  - 命中/核验入口：`Lookup`、`LookupSnapshot`、`HandleEntries`、
    `SnapshotEntries`、`HandleScope`、`SnapshotScope`、`Detached`。
  - 每条字典记录携带 `SourceLabel`（来源标签：`Provenance`、`Scope`、
    `Customer`、`OriginSnapshotID`、`SnapshotID`），用于审计与隔离核验。
- `dictpool_test.go`：使用 Go 内置 `testing` 的验收测试，全部为真实断言。

## 核心规则

1. 公共快照对所有客户可见且永不被写入修改；派生句柄在首次追加前与其共享底层
   存储。
2. 首次私有追加执行写时复制：复制既有条目到独立缓冲区后再追加，公共分支和其他
   客户的句柄观察不到该写入。
3. 保密范围随分支继承：从公共快照派生的句柄初始为 `ScopePublic`，首次追加后变为
   属主私有的 `ScopePrivate`；从私有快照派生的句柄一开始就是私有，且只允许属主
   派生、读取或命中查询。
4. 私有快照不能跨客户串用：跨客户派生、列条目、命中查询一律返回
   `ErrNotAuthorized`。
5. 句柄归还后失效（`ErrHandleReturned`），但其已保存的快照仍可在其保密范围内
   继续使用。

字典条目在本实现中以 `string` 表示字典文本（抽象的字典记录），来源标签与文本
一一绑定。

## 测试覆盖与验证范围

`dictpool_test.go` 覆盖题面的交叉约束：

- **可接受结果**：`TestTwoCustomersSharePublicSnapshot` — 两个客户从同一公共
  快照起步；客户 A 追加私有数据、存私有快照并归还句柄；客户 B 仍命中原公共
  字典且看不到 A 的私有数据；双方后续私有快照均无法被对方派生或查询。
- **应被阻止/拒绝的结果**：`TestCrossCustomerRejections` — 跨客户派生私有分支、
  读取私有快照条目、私有快照命中查询、借用他人句柄追加，均被拒绝且拒绝操作不
  改变任何状态。
- **边界与交错场景**：
  - `TestCopyOnWriteBoundaryAndInterleaving` — 首次追加前后共享/分离边界、写入
    中途再派生句柄、多句柄交错追加、保存快照不影响其他句柄。
  - `TestScopeInheritanceOnDerive` — 私有分支派生的句柄继承私有范围与完整来源
    标签，公共条目标签保持原样。
  - `TestInvalidInputsAndLifecycle` — 空文本、空客户 ID、未知快照/句柄、重复
    归还、归还后调用全部入口的拒绝路径，以及快照比句柄寿命更长的行为。
  - `TestConcurrentAppends` — 并发追加一致性（配合 `-race` 检查数据竞争）。

运行方式：

```sh
go test ./...
go test -race ./...
go vet ./...
```
