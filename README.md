# seqshard — 序列并行分片执行描述生成器

把若干训练样本的 Token 首尾相连成一条全局 Token 流，再按给定的连续区间切给不同
rank（分片边界允许落在样本中间），为每个 rank 生成运行时执行描述：

- 每个 Token 保留它在**原样本内**的位置（`SamplePos`）与值；
- 标签是**本样本的下一个 Token**，样本末位 Token 的标签无效（`Valid=false`），其损失忽略；
- 注意范围只含**本样本内**截至当前位置（含自己）的最近 `W` 个 Token，永不跨样本；
- 对每个 rank，列出为注意计算需从其他 rank 取来的**全局半开区间**，按源 rank 合并
  重叠或相邻段，同一全局 Token 只取一次；
- 标签单独输出，**不计入**注意取段（标签落在其他 rank 也不会产生 fetch）。

## 目录结构

```
go.mod             模块 seqshard，声明 go 1.20，无第三方依赖
seqshard.go        核心实现（仅标准库）
seqshard_test.go   功能测试、参考实现对比、随机化测试与参数校验测试
```

## 公开 API

| 名称 | 说明 |
| --- | --- |
| `Sample{ID string, Tokens []int}` | 一个训练样本，Token 以有序整数值表示 |
| `Range{Start, End int}` | 全局半开区间 `[Start, End)` |
| `Label{GlobalPos, Value int, Valid bool}` | 某 Token 的下一个 Token 标签；末位 `Valid=false` |
| `TokenPlan` | 单 Token 描述：`GlobalPos`、`SampleID`、`SamplePos`、`Value`、`Attention Range` |
| `FetchSegment{SourceRank int, Range Range}` | 需从源 rank 拉取的一段全局区间 |
| `RankPlan` | 单 rank 执行描述：`Rank`、`Owns`、`Tokens`、`Fetch`、`Labels` |
| `Plan` | 全量描述：`Ranks`、`TotalTokens`、`SampleRanges`（每个样本的全局区间） |
| `Build(samples []Sample, cuts []int, window int) (*Plan, error)` | 构建入口；rank `r` 拥有 `[cuts[r], cuts[r+1])` |
| `(*Plan).GlobalTokens() []TokenPlan` | 按 rank 顺序拼回完整全局 Token 流 |
| `*ValidationError` | 非法入参（`W<1`、cuts 不合法、空样本、重复样本 ID 等） |

### 输入约束

- `window`（W）至少为 1；
- `cuts` 首元素为 0、末元素等于 Token 总数，非空流时严格递增；
- 每个样本至少一个 Token，样本 ID 不可重复；
- 空输入只接受 `cuts = [0,0]`，产生一个零 Token 的 rank。

## 关键语义与算法

1. **拼接与分片**：按样本顺序拼接，记录每样本全局 `[start,end)`；rank 按 `cuts`
   拿到连续全局区间，边界可以切开同一样本。
2. **注意窗口**：对全局位置 `pos` 的 Token，设其样本内位置为 `i`，注意区间为
   `[sampleStart+max(0,i-W+1), pos+1)`，天然被样本起点裁剪，不会包含上一样本的 Token。
3. **取段生成**：
   - 先收集本 rank 所有 Token 的注意窗口并做并集合并（相邻 `[a,b)`+`[b,c)` 也合并）；
   - 减去本 rank 自有区间，剩余远程部分与各源 rank 的拥有区间求交；
   - 按源 rank 再做一次重叠/相邻合并并排序，保证同一 Token 至多出现一次。
4. **标签**：样本内非末位 Token 的标签值为同样本下一个 Token；末位 `Valid=false`。
   标签只进 `RankPlan.Labels`，与 `Fetch` 完全隔离。

## 复现步骤

仅需本地 Go 1.20.2 工具链（开发与验证均使用 `go1.20.2 darwin/arm64`），不安装任何
依赖、不访问网络：

```bash
go test ./...          # 功能测试
go test -v ./...       # 查看每个用例
go test -race ./...    # 竞态检测
go test -cover ./...   # 语句覆盖率
```

## 验证范围

所有结论均来自真实执行的 `go test`（最近一次 `-race -cover` 结果：PASS，
**98.3% 语句覆盖**）。测试覆盖题面要求的正常、边界与交错场景：

- **核心场景 `TestBuildLongSampleThreeShardsThenShort`**：10 个 Token 的长样本跨
  三片（cuts `0/4/9/14`），4 个 Token 的短样本紧随其后，W=3。手工断言每 rank 的
  拥有区间、fetch 段（如 rank 2 只从 rank 1 取 `[7,9)`，样本边界处的相邻窗口先合并
  再裁剪）、全部 14 个标签（两个样本末位均为无效标签），并与逐样本参考实现逐项比对。
- **交错分片 `TestInterleavedSplitsAcrossRanks`**：3 个样本各自被边界切开，验证两侧
  边界处理与每 rank 单段 fetch。
- **边界 W=1 `TestWindowOne`**：注意窗口只有自己，跨 rank 标签存在但 fetch 必须为空。
- **大窗口跨多源 rank `TestLargeWindowSpansMultipleRanks`**：一个 rank 同时从两个源
  rank 取数，重叠窗口去重；且注意永不越过样本边界。
- **单 Token 样本 `TestSingleTokenSamples`**：每个 Token 都是末位、标签全部无效。
- **分片与样本边界对齐 `TestCutsAlignedWithSampleBoundaries`**：无远程取段。
- **随机化 200 组布局 `TestRandomizedLayouts`**：确定性 LCG 生成样本数、长度、rank 数、
  切点与 W，每组都做三重独立校验：
  1. 逐样本参考实现（无 rank 概念）重算 Token 身份、注意区间、标签，拼回后逐项相等；
  2. 用独立集合重算每个 rank 应取的远程 Token 集合，与合并后的 fetch 段逐一对应，
     并断言无重复 Token、fetch 不越源 rank 拥有区间、不与自有区间相交；
  3. 全局拼回位置连续且无重复。
- **辅助函数与校验**：区间合并/相减/相交、fetch 合并排序的单测；7 类非法入参返回
  `*ValidationError`；空流 `[0,0]` 正常路径与非法零宽 rank 拒绝；单 rank 全本地化。

### 未执行项

- 本环境对 `go vet`、`go tool cover` 的执行需要额外审批且未获授权，因此未运行；
  静态检查仅以 `gofmt -l`（无输出）与全部 `go test`（含 `-race`）实测结果为准。
- 未做也不需要真实分布式/网络验证：题目只要求生成每 rank 的局部执行描述，所有输入
  均为本地合成数据，输出为纯内存数据结构。
