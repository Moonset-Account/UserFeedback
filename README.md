# mergequeue — 合并队列候选判定模块

纯 Go 标准库实现，不访问 Git 仓库、网络或真实合并队列。模块根据当前主干、队列变更版本、候选登记前缀、实际构建提交和测试回执，判断：

- 哪些候选仍可沿用测试结果；
- 哪些候选因队列中段换版本、队首以不同 SHA 落地、或主干被直接插入提交而需要重建；
- 哪些旧前缀已落地或已从队列消失；
- 队首是否存在**恰好等于当前合并结果**且通过测试的回执。

## 核心模型

```go
type Input struct {
    Trunk      SHA               // 当前主干提交
    Queue      []ChangeVersion   // 当前有序队列及每个变更的精确版本
    Candidates []Candidate       // 候选登记信息
    Receipts   []Receipt         // 测试结果/回执
    HeadCommit SHA               // 可选：外部计算出的队首当前合并结果
}

type Candidate struct {
    ID     string
    Base   SHA               // 候选前缀合并时使用的基线主干
    Prefix []ChangeVersion   // 登记的有序前缀及版本
    Build  SHA               // 实际构建并测试的最终提交
    Steps  []PrefixCommit    // 可选但关键：各前缀长度对应的累计结果提交
}
```

`Steps` 中 `Length: 1` 表示合入第一个登记变更后的提交，`Length: 2` 表示合入前两个后的提交，依此类推。最后一步也可以显式登记，但必须等于 `Build`。

判定以精确 SHA 为准：不会把短 SHA、大小写不同但未规范化的值、等价 tree、patch-id 相同或“看起来是同一个改动”的提交当作同一回执。调用方应先把 Git OID 规范化为同一种形式。

## 公开 API

- `func Adjudicate(in Input) (*Report, error)`：主判定入口；`Judge` 是同义别名。
- `type Report`：包含每个候选的 `Decisions`、重建请求 `Rebuilds`、队首信息和 `Release` 放行结论。
- `func (r *Report) Candidate(id string) (Decision, bool)`：按候选 ID 取判定。
- `func (r *Report) RebuildIDs() []string`：取需要重建的旧候选 ID。
- `func (r *Report) ReuseIDs() []string`：取可直接沿用最终测试结果的候选 ID。
- `func (r *Report) CanReleaseHead() bool`：队首是否可放行。
- `var ErrInvalidInput`：所有输入格式错误均可用 `errors.Is(err, ErrInvalidInput)` 判定。

候选动作：

| Action | 含义 |
| --- | --- |
| `reuse` | 候选仍精确匹配当前状态，且其 `Build` 有通过回执 |
| `wait` | 候选匹配，但缺少通过的最终回执（无回执、pending 或 running） |
| `failed` | 候选匹配，但存在失败回执 |
| `rebuild` | 基线、主干锚点或前缀已失效，需要按 `Report.Rebuilds` 重建 |
| `cancel` | 对应长度的前缀已不存在于当前队列 |
| `retired` | 整个登记前缀的结果提交已经是当前主干，即已落地 |

多个回执指向同一提交时，聚合优先级为：`failed` > `running` > `pending` > `passed`；没有回执为 `missing`。

## 关键规则

### 1. 队列中段被换掉

比较候选登记的 `Prefix` 与当前 `Queue`：

- 第一个不匹配位置之前的前缀仍有效；
- 较短且完全吻合的候选继续复用；
- 从不匹配位置开始覆盖到的较长候选全部标记 `rebuild`；
- `Decision.MatchedPrefixes` 保留仍与当前队列前缀精确对应的提交及其测试状态；
- `Report.Rebuilds` 给出新的 `Base` 和当前队列前缀。

例如 `[A@a1, B@b1, C@c1]` 中的 `B` 换成 `b2`：`A` 的结果保留，`AB` 和 `ABC` 重建为 `[A, B@b2]`、`[A, B@b2, C]`。

### 2. 队首落地提交与中间提交一致

若当前主干等于候选 `Steps` 中第 `k` 步的提交，则认为前 `k` 个登记变更已按该精确提交落地：

- `AnchorLength = k`；
- 剩余登记前缀与当前队列比较；
- 完全吻合时，候选的实际 `Build` 和已有回执可继续使用；
- 对当前队首，模块在后续候选记录的 `k+1` 步提交中寻找当前合并结果。

例如旧候选 `AB` 的构建提交是 `M_AB`，其中第一步记录 `M_A`。当 `A` 以 `M_A` 落地、队列变为 `[B, C]` 且当前主干为 `M_A` 时，`M_AB` 正好是 `B` 合到新主干后的队首结果，因此 `M_AB` 的通过回执可以放行 `B`。

### 3. 队首以不同 SHA 落地

若落地方式产生了新的 SHA（例如 squash/rebase 后提交哈希变化），当前主干既不等于 `Base`，也不等于任何记录的 `Steps`，旧回执不能沿用。模块会根据当前队列对齐剩余前缀长度，并生成 shifted rebuild 请求。

### 4. 直接提交插入主干

若当前主干是直接推送的提交，不等于候选基线或任何已记录步骤，则所有基于旧主干的结果都缺少该直接提交，必须重新判断/重建。即使旧提交的测试全部通过，也不会放行当前队首。

### 5. 队首回执必须精确匹配当前合并结果

- 若提供 `Input.HeadCommit`，它是调用方计算的权威当前队首合并结果；只有该 SHA 的 `passed` 回执能放行。
- 若不提供，模块从所有仍匹配的候选步骤中推导唯一队首提交。
- 推导出多个不同队首提交时返回 `ReasonAmbiguousCommit`，保守地不放行。
- 其他 SHA 的通过回执、失败、运行中、等待中或缺失回执均不放行。

## 用法示例

```go
package main

import (
    "fmt"
    mq "mergequeue"
)

func main() {
    report, err := mq.Adjudicate(mq.Input{
        Trunk: "trunk-sha",
        Queue: []mq.ChangeVersion{
            {ID: "pr-1", Version: "head-v1"},
            {ID: "pr-2", Version: "head-v2"},
        },
        Candidates: []mq.Candidate{{
            ID:    "prefix-2",
            Base:  "trunk-sha",
            Build: "merge-1-2-sha",
            Prefix: []mq.ChangeVersion{
                {ID: "pr-1", Version: "head-v1"},
                {ID: "pr-2", Version: "head-v2"},
            },
            Steps: []mq.PrefixCommit{{Length: 1, Commit: "merge-1-sha"}},
        }},
        Receipts: []mq.Receipt{
            mq.PassedReceipt("merge-1-sha"),
            mq.PassedReceipt("merge-1-2-sha"),
        },
    })
    if err != nil {
        panic(err)
    }

    fmt.Println(report.CanReleaseHead(), report.Release.Commit)
    // true merge-1-sha
}
```

`retired` 或 `cancel` 的候选可由调用方归档，并从下一次快照的 `Candidates` 中移除；测试回执存储仍由调用方负责，`Adjudicate` 是无副作用的纯判定函数。

## 运行方法

环境使用 Go 1.20.2，仅依赖标准库，不需要下载依赖：

```bash
go test ./...
```

其他已执行的检查：

```bash
go test -count=1 -cover ./...
go test -race -count=1 ./...
```

## 验证范围

测试和 `ExampleAdjudicate` 全部使用本地合成 SHA 与变更版本，不访问网络或真实仓库。覆盖：

1. 前一项、前两项、前三项全部匹配并通过时，正确放行队首 `m-a`。
2. 回执边界：缺失、pending、running、failed、其他 SHA 的通过回执、精确 SHA 通过、成功与失败混合、权威 `HeadCommit`。
3. 队列中段 `B@b1 -> B@b2`：保留 `A/m-a` 的通过结果，`AB` 与 `ABC` 标记 `prefix_changed/rebuild`，并校验重建前缀。
4. 完整交错序列：换版本 → 新构建运行中 → 回执到达 → `A` 按记录的中间提交落地 → 沿用 `m-ab2` 放行 `B2` → 直接提交插入主干 → 旧回执失效并按新主干重建 → 新精确回执放行。
5. 正常落地交错：当前主干等于中间提交时，后续候选锚定并复用，队首使用恰好对应的 `m-ab` 回执。
6. 不同 SHA 落地：旧提交无锚点，候选按队列位移重建为 `[B]` 和 `[B,C]`，旧回执不放行。
7. 直接提交插入主干：三个长度全部 `trunk_moved/rebuild`，不保留旧前缀放行结论。
8. 多个候选推导出不同队首 SHA 时保守阻止；提供权威 `HeadCommit` 且有精确通过回执后才放行。
9. 边界：候选比队列长、队列收缩、空队列、空前缀、重复候选 ID、缺失主干/版本、非法回执状态、非法步骤长度和最终步骤不一致。
10. `go test -race` 通过；判定过程不使用包级可变状态。

## 安全说明

本模块只实现题面要求的防御性一致性判断：精确提交匹配、失效前缀重建、歧义阻止放行、输入校验。它不生成或执行利用、入侵、攻击或规避载荷，也不包含任何外部目标访问。
