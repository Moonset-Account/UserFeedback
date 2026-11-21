# capacityreview — 单区故障容量评审

给定各入口的正整数基准请求量、各区域的处理上限，以及每个入口 → 各区域允许转接的整数上限，计算：

1. 所有“恰好一个区域停机”的场景都可承受的最高非负整数倍率 `k`；
2. 在该倍率下，每个故障场景的一份**整数**实际分配（同一入口的流量可以拆分到多个区域）；
3. 再提高一档（`k+1`）时，一个失败场景以及该场景流量网络中容量不足的 s-t 割与割容量。

模块仅使用 Go 1.20.2 标准库，无外部依赖。

## 建模

对每个故障区域 `j` 构造一张流量网络（该区域从网络中整体移除）：

```
source --k*BaseDemand[i]--> entry[i] --TransferLimit[i][z]--> zone[z] --ZoneCapacity[z]--> sink
                                    (z != j)
```

一个场景可承受当且仅当最大流等于总请求量 `k * Σ BaseDemand[i]`。由于容量均为整数，最大流可取得整数，因此得到的分配天然是整数分配。

固定场景的可行性对 `k` 单调（可行流按比例缩小即可），故每个场景存在最大可行倍率，答案是所有场景最大可行倍率的最小值；每个场景用二分 + 最大流判定。若答案 `K` 的瓶颈场景为 `j`，则对 `k = K+1` 的同一网络求最大流，由最大流最小割定理，残量图中 source 可达点集给出最小割，其容量等于最大流且严格小于 `K+1` 档的总请求量——这就是失败证书。

关键性质：只要两个入口都只能转接到同一个备用区，该备用区的容量、以及这两个入口到它的链路限额都会直接出现在割里；网络中没有“把别处空闲容量挪过来”的边，所以全站剩余容量再大也不能让该场景通过。

## 公开 API（package `capacityreview`）

```go
type Problem struct {
    BaseDemand    []int64   // 每个入口的正整数基准请求量
    ZoneCapacity  []int64   // 每个区域的处理上限（非负）
    TransferLimit [][]int64 // [入口][区域] 允许转接的整数上限（非负，0 表示不可直达）
}

func Solve(p Problem) (Result, error)

type Result struct {
    MaxMultiplier  int64
    Scenarios      []ScenarioResult      // 每个故障区一份
    NextMultiplier int64                 // MaxMultiplier + 1
    Failure        *FailureCertificate   // NextMultiplier 档的失败证据
}

type ScenarioResult struct {
    FailedZone int
    Feasible   bool
    Allocation Allocation
}

type Allocation struct {
    Flow       [][]int64 // Flow[入口][区域]，故障区列恒为 0
    ZoneLoad   []int64   // 各区域负载（列和）
    EntryTotal []int64   // 各入口实际发出量（行和）
}

type FailureCertificate struct {
    FailedZone int
    Multiplier int64 // = NextMultiplier
    Required   int64 // 该档总请求量
    MaxFlow    int64 // 该场景最大流，严格小于 Required
    Cut        Cut
}

type Cut struct {
    SourceSide []Node    // 割的源侧节点集合
    SinkSide   []Node    // 割的汇侧节点集合
    Edges      []CutEdge // 所有从源侧跨到汇侧的正容量边
    Capacity   int64     // 割容量，等于 MaxFlow
}

type CutEdge struct {
    From, To Node
    Capacity int64
}

type Node struct {
    Kind  NodeKind // NodeSource / NodeEntry / NodeZone / NodeSink
    Index int      // entry/zone 下标
}
```

`Solve` 不修改输入；参数非法（空入口/空区域、非正基准、负容量或负限额、行列数不匹配、int64 溢出）返回 error。

## 复现

工作区即模块根目录（`go.mod` 声明 `go 1.20`）：

```sh
go version          # go1.20.2
gofmt -l .          # 实际执行，输出为空
go test ./...       # 实际执行，全部通过
go test -run Example -v ./...   # 实际执行，含真实断言输出
```

可选静态检查（本次未执行 `go vet ./...`，因运行环境未授权该命令；已执行 `go test -vet=all ./...` 并全部通过）：

```sh
go vet ./...
```

## 测试与验证范围

`solver_test.go` 中的真实测试（`go test ./...` 全部通过，已实际执行）：

| 测试 | 场景类型 | 断言内容 |
| --- | --- | --- |
| `TestSimpleThreeZones` | 正常 | `K=3`；3 个场景分配合法；`k=4` 时割容量 6 = 最大流 < 请求量 8 |
| `TestTransferLimitsAndCapacity` | 正常 + 交错 | 共享备用区 Z1 容量 4 卡住 `K=4`；割精确为 `zone[1]->sink(4)` + `source->entry[1](5)` |
| `TestSharedBackupZoneCannotUseAggregateCapacity` | 关键边界/交错 | 两入口只能去同一备用区 Z2（容量 1），全站容量 201 也只得 `K=1`；失败档请求 4、最大流 3，割含 `zone[2]->sink(1)`；防止用全站剩余容量放行 |
| `TestEntrySplitsAcrossZones` | 交错 | 单入口 10 单位被拆分到两个存活区（各 6 上限），行和精确为 10 |
| `TestPerLinkLimitIsTheCut` | 边界 | 区域很空闲但入口→区域链路限额 3 成为瓶颈；割中入口→区域边合计 3 |
| `TestSingleZoneAlwaysInfeasibleForPositiveDemand` | 边界 | 只有一个区域时对任何正请求量都不可承受，`K=0`；失败档割为空、容量 0 |
| `TestZeroMultiplierAlwaysFeasible` | 边界 | 区域容量全 0 时 `K=0`，各场景零分配仍合法 |
| `TestInputNotMutated` | 边界 | `Solve` 不改输入且两次调用结果一致 |
| `TestLargeButRepresentableDemand` / `TestAggregateOverflowIsReported` | 边界 | 2^62 量级仍正确；聚合溢出返回错误 |
| `TestValidationErrors` | 边界 | 8 类非法输入均返回 error |
| `TestRandomizedAgainstReference` | 随机交叉验证 | 300 个随机小实例与**独立的穷举分配 oracle** 对照：最大倍率、每场景分配合法性、失败场景在 `K` 可行且在 `K+1` 不可行、割容量 = 最大流 < 请求量 |
| `TestRandomizedDenseSmall` | 随机交叉验证 | 另外 150 个 2–3 入口 / 3–4 区域的小实例同上对照 |
| `ExampleSolve` | 示例 | 输出作为文档示例；期望输出由真实运行得到，且同时被测试校验 |

辅助断言函数：

- `validAllocation`：逐项检查流量非负、故障区列为 0、不超链路限额、行和等于 `k*BaseDemand[i]`、列和不超过区域容量且与 `ZoneLoad` 一致。
- `validCut`：从零重建场景网络的全部正向边，独立验证 `SourceSide`/`SinkSide` 是不相交划分、source 在源侧、sink 在汇侧、故障区不在网络中，`Edges` 恰为所有跨割正容量边且容量与 `Capacity` 一致。
- `validCertificate`：用穷举 oracle 复核“该场景在该倍率确实不可行”，并检查 `Required = k*ΣBaseDemand`、`MaxFlow < Required`、`Cut.Capacity = MaxFlow < Required`。

### 未覆盖/未执行

- 未执行 `go vet ./...`（运行环境未授权该命令）；改用 `go test -vet=all ./...`，已通过。
- 未做大规模性能压测；二分 + Dinic 的复杂度对常规评审规模足够，但未给出吞吐基准。
- 未提供 CLI 或文件输入解析；调用方直接使用 `Solve`。
- 未验证 `go test -race` 以外的并发使用；`Solve` 无共享可变状态，可并发调用，但未写专门并发测试。
