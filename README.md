# symbind — 插件运行时符号绑定解析器

在已解析的模块表上做符号绑定。每个模块有命名空间、按声明顺序排列的依赖、
带版本的强/弱导出，以及一份允许跨命名空间引用的具体模块清单（manifest）。

## 解析规则

- **可见闭包与查看次序**：从请求模块出发按层（BFS）遍历依赖闭包，同层内按
  依赖声明顺序入队；模块自身为第 0 层。
- **版本匹配**：显式版本只接受同版本导出；未指定版本只绑定标成 `Default`
  的导出。
- **强弱裁决**：匹配的强定义优先于弱定义（跨层也成立）；同强度并列时由
  查看次序（先扫到者）裁决。
- **跨命名空间**：仅当**声明该依赖的模块**在其 manifest 中登记了目标模块时
  边才开放，且只开放清单列出的符号；未开放的边记入 `Resolution.Blocked`。
  进入外部模块后，后续依赖仍按各自模块自己的命名空间规则逐跳判定
  （同空间边全开，跨空间边需该模块自己的 manifest 条目）。
- **成环终止**：visited 集合保证依赖环（含自环）上的查询必然结束。
- **可复核结果**：返回选中的定义、从请求者到定义模块的可见路径
  （`Path`）、实际扫描顺序（`Scanned`）与被拒绝的边（`Blocked`）。

## 公开 API

```go
type Export     { Symbol, Version string; Strong, Default bool }
type Module     { Name, Namespace string; Deps []string; Exports []Export; Allow map[string][]string }
type Request    { Symbol, Version string }              // Version 为空 = 未指定版本
type Resolution { Found bool; Module string; Export Export; Path, Scanned, Blocked []string }

func NewTable() *Table
func (*Table) AddModule(m Module) error                 // 拒绝空名/重名
func (*Table) Resolve(from string, req Request) (Resolution, error)
```

`Allow` 的键是外部依赖模块名（出现即开放该跨空间边），值是允许被引用的
符号列表。`Resolve` 对未知请求模块和悬空依赖返回错误。

## 运行方法

需要 Go 1.20（开发环境为 go1.20.2 darwin/arm64）。无第三方依赖。

```sh
go test ./...        # 运行全部测试
go test ./... -v     # 查看各场景明细
```

## 验证范围（`symbind_test.go`，全部实测通过）

| 测试 | 覆盖点 |
|---|---|
| TestStrongBeatsWeakAcrossLayers | 强定义跨层优先于先扫到的弱定义；路径与扫描顺序断言 |
| TestViewOrderBreaksTies | 同强度并列：同层按声明顺序、浅层优先于深层 |
| TestExplicitVersionMatchesSameVersionOnly | 显式版本只匹配同版本；缺失版本不绑定 |
| TestUnspecifiedVersionUsesDefaultExport | 未指定版本只走 Default 导出；无 Default 则不绑定 |
| TestWeakOnlySelected | 仅弱定义时选中弱定义 |
| TestDependencyCyclesTerminate | 相互依赖环 + 自环：命中与不命中的查询都终止 |
| TestCrossNamespaceManifest | 无 manifest 拒绝并记录 Blocked；清单外符号保持关闭 |
| TestCrossNamespaceTransitiveRules | 进入外部模块后按各自空间规则逐跳判定（同空间全开、跨空间需自身 manifest条目） |
| TestBlockedModuleReachableViaLegitimateEdge | 被拒模块可经后续合法边到达（交错情形） |
| TestInterleavedCrossNamespaceVersionStrength | 跨空间限制 × 版本选择 × 强弱优先交错 |
| TestUnknownModules / TestAddModuleValidation | 未知模块、悬空依赖、空名/重名报错 |

说明：`go vet ./...` 因环境权限未单独执行；`go test` 默认已运行其 vet
子集（printf、buildtags 等检查），全部通过。`gofmt -l` 无输出。
