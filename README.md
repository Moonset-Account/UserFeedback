# 保守回填批作业调度器（可推进虚拟时钟）

一个零依赖的 Go 1.20.2 离散事件调度器，模拟批计算集群的槽位分配：

- 作业按到达次序排队，每个作业必须**一次性**拿到指定数量的槽位；
- 作业启动后**不可抢占**，`Runtime` 是可靠运行时间上界，用于计算保留时刻；
- 队首因槽位不足等待时，按已运行作业的最迟必要结束时刻，计算队首**最早可保证的开工时刻（保留时刻）**；
- 后续作业按队列顺序尝试**保守回填**：只有现在放得下、且加入后队首仍能在保留时刻取得足够槽位时才允许启动；
- 作业可通过 `ActualRuntime` 模拟**提前结束**，从而让队首提前开工（每次调度都基于仍在运行的作业重算保留时刻）；
- 同一时刻**先归还所有已结束作业的槽位，再进行调度**；
- 一个后来的长作业可以跨过保留时刻运行，只要那时由其他作业空出的槽位仍够队首使用。

## 目录结构

| 文件 | 说明 |
| --- | --- |
| `go.mod` | 模块 `backfillscheduler`，仅声明 Go 1.20，无第三方依赖 |
| `scheduler.go` | 调度器全部实现（标准库 `errors`、`sort`） |
| `scheduler_test.go` | 12 个功能测试，全部含真实断言 |
| `README.md` | 本文件 |

## 公开 API

类型：

- `Job`：作业描述。
  - `ID string`：唯一标识；
  - `Arrival int64`：到达时刻（非负整数时间单位）；
  - `Slots int`：所需槽位，必须满足 `1 <= Slots <= 集群总槽数`；
  - `Runtime int64`：可靠运行时间上界（正数），驱动保留时刻计算；
  - `ActualRuntime int64`：实际运行时长；`<= 0` 表示取 `Runtime`，正数表示提前结束，但不得超过 `Runtime`。
- `Result`：作业完成记录，含 `JobID / Arrival / Start / Finish / Slots / Estimate / Actual`。

方法：

- `New(totalSlots int) (*Scheduler, error)`：创建指定总槽位的调度器。
- `Submit(j Job) error`：提交作业；校验空 ID、重复 ID、非法槽位/时长/到达时间、实际时长超上界等。
- `Step() (int64, bool)`：把虚拟时钟推进到下一个事件（到达或完成），先归还该时刻全部完工槽位、再放入到期作业并执行一次调度；无事件时返回 `false`。
- `Run() []Result`：反复 `Step` 直到所有作业完成，返回按完成顺序排列的结果。
- `Now() int64`：当前虚拟时间。
- `FreeSlots() int`：当前空闲槽位。
- `PendingCount() int`：尚未启动的作业数（含未到达者）。
- `RunningCount() int`：正在运行的作业数。
- `Reservation() (jobID string, at int64, ok bool)`：当前队首的保留时刻；队首可立即开工或无等待队首时 `ok == false`。
- `Results() []Result`：已完成作业的副本（完成顺序）。
- `ResultOf(id string) (Result, bool)`：按 ID 查询单个完成记录。
- `Idle() bool`：无运行、无等待、未到达作业均为空。

## 核心语义

1. **保留时刻**：队首需求为 `need`、当前空闲为 `free` 时，把运行中作业按估计结束时刻升序排列，逐批累加其释放的槽位，直到累计空闲 `>= need`；此时对应的估计结束时刻即保留时刻（即“最迟的那个必要结束时刻”）。
2. **回填判定 `preserves`**：扣除回填作业占用后，统计保留时刻之前（含该时刻）所有运行作业——以及回填作业自身若估计结束不晚于保留时刻——能释放的槽位；总数仍 `>= 队首需求` 才允许回填。因此跨保留时刻的长作业在槽位仍够时可以开工，不够时被拒绝。
3. **同刻顺序**：`Step` 先一次性收走所有 `actualEnd <= 当前时刻` 的作业，再搬入到达作业，最后调度，杜绝“同刻先占后还”。
4. **提前结束**：`actualEnd` 按 `ActualRuntime` 计算而保留时刻按 `Runtime` 上界计算；作业提前完工后下一次调度重算，队首可早于原保留时刻启动。

## 复现步骤

要求本机 Go 1.20.2（本工作区已验证 `go version go1.20.2 darwin/arm64`），无需联网、无需安装依赖：

```bash
go test ./...
go test -race -v -count=1 ./...
```

## 验证范围（均为真实测试，无预置演示输出）

正常流程：

- `TestNormalStartAndReservation`：队首等待并得到保留时刻 10，短作业回填 4 槽；完工、再调度、最终起止时刻逐项断言。
- `TestReservationIsLatestRequiredEnd`：需要多个作业陆续释放才能凑齐槽位时，保留时刻取其中最迟的必要结束时刻（8，而非最早完工的 5）。

边界场景：

- `TestBackfillCrossingReservationDenied`：跨保留时刻且届时槽位不够队首的回填被精确拒绝。
- `TestBackfillEndingExactlyAtReservation`：估计结束时刻恰好等于保留时刻（`<=` 边界）允许，多一个时间单位则拒绝。
- `TestWholeClusterJobBoundary`：单作业占满全部槽位时无任何回填可能。
- `TestStepStopsWhenDrained`：时钟不会越过最后一个事件，排空后 `Step` 返回 `false`。
- `TestValidation`：零/负槽位、超总槽数、零时长、负到达、实际时长超上界、重复 ID 等非法输入全部报错。

交错场景：

- `TestBackfillCrossingReservationAllowed`：题面明确规则——后来的长作业跨过保留时刻，只要其他槽位仍够队首即可开工。
- `TestEarlyFinishAdvancesHead`：实际上界内提前完工使队首早于原保留时刻启动，保留状态随之清除。
- `TestSameTimestampReturnThenSchedule`：两个作业同刻完工，槽位全部归还后 10 槽大作业在同一时刻启动。
- `TestArrivalWhileHeadWaits`：等待期间新到达者进入回填队列且不打乱队首保留；无空槽时继续排队。
- `TestBackfillQueueOrder`：严格按队列顺序尝试回填，放不下的作业被跳过而其后更小的作业仍可回填。

未执行项：`go vet` 在本环境需要额外授权而未运行；除此之外，格式化检查（`gofmt -l` 无输出）、普通测试与 `-race` 竞态检测均已实际执行并通过。
