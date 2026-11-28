# depmatcher — 发布流水线制品依赖匹配器

发布流水线中的每件制品由**平台、编译器、特性**等维度属性描述。本包实现一个
依赖匹配器：给定已产出的带维度清单、维度相等/可替代规则、条件排除规则以及测试
包各槽位的基数要求，返回一组填满所有必需位置的完整匹配；无解时给出需求数超过
兼容制品数的槽位及其候选集合。

## 设计要点

- **制品即实物**：每个 `Artifact` 有唯一 id。一个测试包的每个必需位置都必须由
  独立实物填满——同一制品不能在同一绑定内被两个位置（无论是否同槽）占用。
- **槽位基数**：`SlotRequirement.count` 要求几件就占几个位置，候选按 id 排序后
  以组合方式枚举。
- **标量维度**（`platform`、`compiler` 等）：支持相等匹配与**有向、可传递**的
  替代规则 `(dimension, from_value, to_value)`——需求 `from_value` 可由携带
  `to_value` 的制品满足，反向不成立。
- **集合维度 `features`**：采用包含语义，制品特性集必须包含全部要求特性。
- **条件排除 `Exclusion`**：制品侧模式与测试包侧模式同时命中时，该配对被剔除；
  空模式充当通配。
- **匹配过程三阶段**：
  1. 逐槽基数检查，候选数 < 需求数即记入 `SlotShortage`（含完整候选集合）；
  2. MRV（候选最少优先）回溯 + 全局占用集合，保证跨槽实物独立，结果按 id 序
     确定性产出；
  3. 若各槽单独都可行但全局冲突，按 Hall 条件找出**最小**（再按槽名字典序）
     的违规槽组，以 `Contention` 返回其合并候选集与总需求。
- **绑定重放**：匹配成功后可存入 `BindingRegistry`。后续清单新增制品时，之前已
  成功的无关测试包用**原绑定**重放而非重新分配；新增制品同时可修复此前失败的
  包。制品被移除、槽位拓扑变化或兼容性丧失时，重放返回失败并给出原因。

## 目录结构

```
depmatcher/
  __init__.py   公开 API 导出
  model.py      Artifact / TestPackage / SlotRequirement / Exclusion / 约束 / 异常
  rules.py      RuleBook：相等、替代闭包、特性包含、排除、逐槽候选筛选
  matcher.py    Inventory / DependencyMatcher / MatchResult / SlotShortage / Contention
  binding.py    Binding / BindingRegistry / ReplayResult
tests/
  fixtures.py      本地合成的流水线数据（8 件制品、4 个测试包）
  test_rules.py    规则层单测
  test_matcher.py  匹配引擎单测
  test_replay.py   绑定存储与重放单测
  test_pipeline.py 端到端叙事测试
```

仅使用 Python 3.11 标准库，无第三方依赖。

## 公开 API

| 符号 | 用途 |
| --- | --- |
| `Artifact(id, kind, dimensions, features)` | 一件已产出制品 |
| `TestPackage(id, slots, attributes)` | 测试包；`attributes` 供排除规则按包匹配 |
| `SlotRequirement(name, kind, count, constraints)` | 一个必需槽及基数 |
| `MatchValue(dimension, value)` / `MatchAnyOf(dimension, values)` / `MatchAny(dimension)` | 槽内维度约束（AND 组合） |
| `Exclusion(name, artifact_match, package_match)` | 条件排除规则 |
| `RuleBook(substitutions, exclusions)` | 替代规则（三元组）与排除规则集合 |
| `Inventory(artifacts)` / `.add(artifact)` / `.get(id)` / `.all` | 带 id 索引的制品清单 |
| `DependencyMatcher(rule_book).match(package, inventory)` | 返回 `MatchResult` |
| `MatchResult.success / binding / shortages / contention` | 成功绑定或失败诊断 |
| `SlotShortage.slot / required / candidates` | 需求超候选的槽及其候选集 |
| `Contention.slots / required_total / candidates` | Hall 违规槽组及合并候选集 |
| `BindingRegistry(rule_book).store(result)` / `.replay(package, inventory, matcher)` | 绑定持久化与原绑定重放 |
| `ReplayResult.valid / binding / reasons` | 重放结论与失效原因 |
| `DimensionError` | 非法输入（缺维度、重复 id、基数 < 1 等） |

### 最小示例

```python
from depmatcher import (
    Artifact, Inventory, RuleBook, DependencyMatcher,
    TestPackage, SlotRequirement, MatchValue, BindingRegistry,
)

rules = RuleBook(substitutions=[("compiler", "gcc11", "gcc12")])
matcher = DependencyMatcher(rules)
inventory = Inventory([
    Artifact("rt-1", "runtime", {"platform": "linux"}),
    Artifact("cc-1", "compiler", {"compiler": "gcc12"}),
])
package = TestPackage("pkg", (
    SlotRequirement("runtime", "runtime",
                    constraints=(MatchValue("platform", "linux"),)),
    SlotRequirement("toolchain", "compiler",
                    constraints=(MatchValue("compiler", "gcc11"),)),
))

result = matcher.match(package, inventory)
assert result.success
# {'runtime': ('rt-1',), 'toolchain': ('cc-1',)}  # gcc12 替代满足 gcc11

registry = BindingRegistry(rules)
registry.store(result)
inventory.add(Artifact("cc-2", "compiler", {"compiler": "gcc12"}))
replay = registry.replay(package, inventory, matcher)
assert replay.valid and replay.binding["toolchain"] == ("cc-1",)  # 原绑定不变
```

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

隔离解释器（`-I` 不自动注入当前目录，需显式指定路径）：

```bash
python3 -I -c "import sys; sys.path[:0] = ['.', './tests']; \
import unittest; r = unittest.TextTestRunner(verbosity=2).run( \
unittest.TestLoader().discover('tests')); sys.exit(not r.wasSuccessful())"
```

## 验证范围（全部基于本地合成数据，无联网、无外部依赖）

共 **46** 个测试用例，实测全部通过：

- **规则层（test_rules.py，20 例）**：相等匹配；直接与传递替代、反向不成立；
  `features` 包含语义与部分缺失拒绝；`MatchAnyOf`/`MatchAny` 分支及替代闭包；
  多约束 AND；制品 kind 过滤；条件排除的包侧/制品侧双条件、标量与特性模式，
  含同一制品仅对 debug 包排除的交错场景。
- **匹配引擎（test_matcher.py，15 例）**：完整匹配与跨槽独立实物；替代兜底；
  空槽测试包；确定性重复匹配；基数短缺并精确报告候选集；`count=2` 占两个位置；
  超量需求；Hall 争用诊断；三槽全局独立性；回溯修正贪心首选；重复 id、缺失
  维度、非法基数等异常路径。
- **绑定重放（test_replay.py，9 例）**：成功绑定存取；失败结果禁止入库；**新增
  制品后原绑定逐字重放**；新增制品修复此前失败包；移除制品后重放失效；无绑定
  重放；槽位拓扑变化检测；绑定制品 id 消失检测；绑定内实物重复拒绝。
- **端到端（test_pipeline.py，2 例）**：成功 → 基数失败 → Hall 失败 → 补制品后
  失败包转成功且无关包保持原绑定的完整交错故事线；排除与替代规则交错生效。
- **附加实测**：`python3 -m compileall` 全量字节码编译通过；独立脚本验证了
  “短缺诊断优先于争用诊断”和“最小 Hall 违规组选取”两条边界（验证后脚本已删除）。

## 已知边界与取舍

- Hall 诊断枚举槽组子集，槽位数量极大时为指数复杂度；当前面向流水线规模
  （数十槽以内）设计，回溯同样以正确性和确定性优先。
- 替代规则只作用于标量维度；`features` 仅支持包含匹配，不做特性间替代。
- 重放只校验已存储绑定在当前清单/规则下是否仍成立，不会在失效时自动重新匹配。
