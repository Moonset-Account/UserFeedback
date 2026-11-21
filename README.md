# procruntime — 进程描述符表与匿名管道模型

纯 Go（仅标准库）实现的内存模型，模拟进程运行时中与匿名管道相关的描述符表
操作：建管道、把描述符复制到指定编号、设置执行时关闭（close-on-exec）标记、
派生子进程、替换程序、关闭描述符以及读写管道。模型不访问真实操作系统、网络或
文件系统，所有对象都在进程内用引用计数模拟。

- Go 版本：**Go 1.20.2**（`go.mod` 声明 `go 1.20`，无外部依赖）
- 模块名：`procruntime`，包名同模块名

## 语义说明

### 描述符表与共享端点

- 每个进程（`Process`）持有独立的描述符表 `map[fd]*fdEntry`，表项记录它指向
  的管道端点（读端/写端）以及 close-on-exec 标记。
- 管道（`pipeObject`）是父子进程共享的底层对象，内部维护字节缓冲与两端的
  引用计数：每有一个打开的读端描述符，`readers` 加一；每有一个打开的写端
  描述符，`writers` 加一。
- 新描述符从当前**最低空闲编号**开始分配。

### 各操作语义

| 操作 | 语义 |
| --- | --- |
| `Pipe` | 新建管道，返回最低空闲的两个描述符 `(读端, 写端)`，均不带标记，两端初始引用各为 1。 |
| `Dup2(pid, oldfd, newfd)` | 复制到指定编号：若 `newfd` 已打开则先关闭它（释放旧引用）；新描述符的 close-on-exec 标记**清除**；`oldfd == newfd` 时为空操作，**保留原标记**。 |
| `SetCloseOnExec` | 设置或清除指定描述符的执行时关闭标记。 |
| `Fork` | 派生子进程：完整复制描述符编号、端点类型与标记；底层管道端点共享，每复制一个描述符对应引用计数加一；子进程初始运行同一程序。 |
| `Exec` | 替换程序：所有带标记的描述符被关闭并释放引用；未打标的描述符（包括打标描述符的未打标副本）保留。 |
| `Close` | 关闭一个描述符并释放其端点引用；重复关闭返回 `ErrBadFD`。 |
| `Read` | 先取走缓冲字节（FIFO，最多 `len(p)`）；缓冲为空且仍有写端时返回 `ErrNoDataAvailable`；缓冲为空且所有写端均已关闭时返回 `io.EOF`。 |
| `Write` | 向写端追加全部字节；所有读端均已关闭时返回 `ErrBrokenPipe`。 |

### 空读的两种情况（关键区分）

- **暂缺数据**：管道缓冲为空，但 `writers > 0`，返回
  `ErrNoDataAvailable`（非 EOF，之后写入的数据仍可读到）。
- **写入结束**：管道缓冲为空且 `writers == 0`，返回标准库的 `io.EOF`。

典型交错场景（对应测试 `TestInterleavedForkDupExecCloseEOF`）：父进程关闭
全部写端后，子进程执行一次 `Exec` 只会关掉带标记的写端，未打标的副本仍然
打开，因此空读依旧是“暂缺数据”；直到该副本也被关闭，空读才返回 EOF。

## 公开 API

构造与进程管理：

- `NewRuntime() *Runtime`
- `(*Runtime).Start(program string) *Process`
- `(*Process).PID() int`
- `(*Runtime).Fork(parentPID int) (childPID int, err error)`
- `(*Runtime).Exec(pid int, program string) error`

描述符操作：

- `(*Runtime).Pipe(pid int) (readFD, writeFD int, err error)`
- `(*Runtime).Dup2(pid, oldfd, newfd int) error`
- `(*Runtime).SetCloseOnExec(pid, fd int, closeOnExec bool) error`
- `(*Runtime).Close(pid, fd int) error`
- `(*Runtime).Read(pid, fd int, p []byte) (int, error)`
- `(*Runtime).Write(pid, fd int, p []byte) (int, error)`

查询（供测试与调用方观察状态）：

- `(*Runtime).Program(pid int) (string, error)`
- `(*Runtime).FDs(pid int) ([]int, error)`
- `(*Runtime).IsOpen(pid, fd int) (bool, error)`
- `(*Runtime).IsCloseOnExec(pid, fd int) (bool, error)`
- `(*Runtime).FDEnd(pid, fd int) (string, error)`，返回常量 `ReadEnd` / `WriteEnd`

错误哨兵（用 `errors.Is` 比较）：

- `ErrNoSuchProcess`：未知进程号
- `ErrBadFDNumber`：非法描述符编号（如负数）
- `ErrBadFD`：描述符未打开
- `ErrWrongMode`：对写端读 / 对读端写
- `ErrBrokenPipe`：所有读端关闭后再写
- `ErrNoDataAvailable`：空管道且仍有写端（暂缺数据，区别于 `io.EOF`）

## 复现步骤

```sh
# 确认工具链（要求 go1.20.2）
go version

# 在本目录运行
go test -v ./...          # 功能测试，含真实断言
go test -race ./...       # 竞态检测
gofmt -l .                # 格式检查，无输出即通过
```

## 验证范围

`runtime_test.go` 中全部为行为级真实断言（无预置演示输出），共 12 个测试：

正常场景：

- `TestPipeBufferedReadAndEmptyStates`：写入后分批读取、空管暂缺数据、
  关闭最后写端后 EOF
- `TestBufferedBytesDeliveredBeforeEOF`：已缓冲字节先排空，EOF 只在排空后返回
- `TestForkCopiesDescriptorsAndSharesPipe`：fork 复制编号/端点/标记，父子共享
  管道（父写子读）

边界场景：

- `TestDup2OntoSelfKeepsMarker`：复制到自身保留原标记
- `TestDup2ReplacesTargetAndClearsMarker`：复制到已打开编号先替换旧引用，新
  描述符标记清除；旧端点引用计数正确下降（触发 broken pipe）
- `TestWriteBrokenPipeAndModeErrors`：读端全关后写返回 `ErrBrokenPipe`，
  读写模式不匹配返回 `ErrWrongMode`
- `TestDescriptorAndProcessErrors`：未知进程、未打开/负数描述符、重复关闭
- `TestCloseReleasesLowestFreeNumber`：关闭后编号从最低空闲位复用

交错场景：

- `TestForkMarkersAreIndependentPerProcess`：fork 后父子标记相互独立
- `TestExecClosesOnlyMarkedDescriptors`：exec 只关闭带标记描述符，未打标副本
  存活
- `TestInterleavedForkDupExecCloseEOF`：父关全部写端 → 子 exec 留下未打标副本
  → 空读仍为暂缺数据 → 副本关闭后空读才 EOF（题面核心场景）
- `TestConcurrentAccessSmoke`：并发写与标记切换的加锁冒烟测试（配合
  `-race` 验证）

本地实际执行结果（Go 1.20.2 darwin/arm64）：`go test -v ./...` 12/12 PASS；
`go test -race ./...` 通过；`gofmt -l .` 无输出。

## 未覆盖 / 未执行项

- 未模拟真实系统调用、信号、阻塞 I/O 调度与管道容量上限（写为无限缓冲模型）。
- 未包含真实操作系统级进程或文件描述符；不涉及任何网络访问。
- 本环境中 `go vet` 的执行权限未获批准，因此 **vet 未执行**；编译与全部测试
  （含竞态检测）均已实际通过。
