# admission — 监控采集准入模块

在**团队**和**单指标**两个维度上限制活跃时间序列数量，对输入的样本批次逐条给出接收结果。

## 核心语义

- **序列身份** = `(团队, 规范化指标名, 清洗后的标签组)`。两条样本清洗后标签组相同
  （且团队、规范化指标名相同）就共用一条序列；不在 `KeepLabels` 名单里的标签被丢弃，
  不影响身份。
- **别名共用**：通过 `RegisterAlias` 登记的新名与旧名解析为同一规范化名，使用任一名字
  的样本共用序列；未登记的新名属于全新身份。只有新增身份才占用预算。
- **序列退出**：确认时刻减去最近一次新鲜样本的确认时刻，**严格大于** `StaleAfter` 时
  序列退出并释放预算；恰好等于时仍然活跃。
- **历史写入**：确认时刻减去样本时刻，**严格大于** `LateAfter` 时样本被判定为明显迟到，
  记为历史写入——不创建/刷新序列、不占用预算，当前活跃数保持不变。
- **预算**：`TeamBudget` / `MetricBudget` 未配置的团队或指标不受限；新增身份需同时
  通过两项检查，任一不满足即被拒并记录被卡住的预算项。

## 公开 API

| 名称 | 说明 |
| --- | --- |
| `Config` | 配置：`KeepLabels`、`TeamBudget`、`MetricBudget`、`StaleAfter`、`LateAfter` |
| `New(cfg Config) *Controller` | 创建准入模块 |
| `(*Controller).RegisterAlias(canonical, alias string) error` | 登记指标别名；冲突登记返回错误 |
| `(*Controller).Admit(samples []Sample, confirmedAt time.Time) BatchResult` | 对批次做准入判定，确认时刻用于迟到与退出判定 |
| `(*Controller).ActiveCount() int` | 当前活跃序列总数 |
| `(*Controller).TeamActive(team string) int` | 某团队当前活跃序列数 |
| `(*Controller).MetricActive(metric string) int` | 某指标（可为别名）当前活跃序列数 |
| `(*Controller).HistoricalWrites() int` | 累计历史写入样本数 |
| `Sample` | 输入样本：`Team`、`Metric`、`Labels`、`Time` |
| `Decision` | 判定结果：`Accepted` / `Historical` / `Rejected` |
| `SampleResult` | 单条结果：`Decision`、`Identity`、`NewIdentity`、`BlockedBy` |
| `BatchResult` | 批次结果：`Results`（与输入一一对应）、`NewIdentities`、`Blocked`（去重）、`Historical` |
| `BudgetRef` | 被卡住的预算项：`Kind`（`team`/`metric`）、`Key`、`Limit`、`Used` |

## 运行方法

需要 Go 1.20（开发验证使用 go1.20.2）：

```sh
go test ./...        # 运行全部测试
go test ./... -v     # 查看逐条用例
go vet ./...         # 静态检查
```

## 验证范围

测试全部使用本地合成数据（固定时钟、构造的样本批次），未访问任何外部目标。
覆盖情况：

- **正常场景**：清洗后标签组相同的样本共用序列（多余标签被丢弃）；批次结果与输入一一对应。
- **别名**：旧名与已登记新名共用身份；未登记新名是新身份；别名冲突登记报错、幂等重登记成功。
- **边界**：
  - 团队/指标预算恰好用满时可准入，再新增即被对应预算项卡住（`BudgetRef` 报告 `Used/Limit`）；
  - 恰好 `StaleAfter` 未见新鲜样本时序列仍活跃，严格超过才退出并释放预算；
  - 恰好 `LateAfter` 不算迟到，严格超过才记历史写入。
- **迟到样本**：不创建身份、不占预算、活跃数不变，且不刷新已有序列的保活时间（序列按原计划退出）。
- **交错批次**：同一批内混合已有身份、别名新身份、团队预算超限、另一团队新身份与超限、
  明显迟到样本、批内先准入后被共用的身份；断言逐条判定、新增身份数、去重后的被卡预算项、
  历史写入计数与最终活跃数。
- **未配置预算**的团队/指标不受限（50 个新身份全部准入）。
