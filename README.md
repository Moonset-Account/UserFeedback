# include-cache：头文件包含解析缓存与重查

某构建步骤按目录顺序查找头文件。本组件在首次构建时记录每个编译入口的
include 解析过程，之后在新源码树上重查，判定哪些编译产物可以复用、
哪些入口需要重编，并给出从入口到首次差异的一条依赖链。

## 文件

- `include_cache.py` — 组件实现（仅依赖 Python 标准库）。
- `test_include_cache.py` — 合成数据单元测试（**未在本环境运行**，见下文）。

## 模型

- **源码树** `SourceTree`：以 `{路径: 内容}` 形式提供，路径经
  `posixpath.normpath` 规范化，内容为字节串（字符串自动按 UTF-8 编码）。
- **编译选项**：从选项列表中解析 `-I dir` / `-Idir`，得到按顺序的
  include 查找目录。
- **include 解析规则**：
  - `#include "name"`：先从包含者（includer）所在目录相对查找，
    再按 `-I` 目录顺序查找；
  - `#include <name>`：只按 `-I` 目录顺序查找；
  - 命中即停止，命中之后的后备候选不记录。

## 记录（首次构建）

`IncludeCache.record(entry, options, tree)`：

- 记录入口路径、编译选项、入口内容摘要；
- 深度优先展开全部 `#include`（嵌套 include 同样参与），每次解析记录一个
  `IncludeEvent`：包含者、include 名称、是否引号包含、按查找顺序截止到
  命中项的候选列表（路径 + 存在性 + 内容摘要）、命中路径；
- 每个事件记录其父事件下标，用于还原依赖链；
- **循环包含**：命中文件若已在当前包含栈中，只记录事件、不再展开，
  递归必然结束（自包含、互相包含均覆盖）。

关键取舍：**只记录命中位置及其之前的候选**。命中之后的后备文件从未
被选中，其单独变化不影响编译结果，重查时自然被忽略。

## 重查（新源码树）

`IncludeCache.query(entry, tree, options=None)` 按记录顺序重放事件，
在新树上逐项比较：

| 情形 | 差异类型 | 说明 |
| --- | --- | --- |
| 编译选项变化 | `options-changed` | 仅在调用方传入 options 时比较 |
| 入口缺失 / 内容变化 | `entry-missing` / `entry-changed` | |
| 命中点之前新出现同名文件 | `earlier-candidate-appeared` | 靠前目录新增文件使命中前移 |
| 原命中文件缺失 | `hit-missing` | |
| 原命中文件内容变化 | `hit-changed` | |

注意“命中前移”一条：**即使新出现的文件与原命中文件字节完全相同，
也判定为需要重编**——命中路径改变后，下一级引号 include 的相对
查找起点就变了，解析结果可能不同。

`QueryResult` 包含：

- `rebuild`：是否需要重编；
- `difference`：首次差异的描述（类型、路径、事件下标、详情）；
- `chain`：从入口到首次差异的依赖链，例如：

  ```
  entry: src/main.c
  src/main.c: #include "util.h" -> src/util.h
  src/util.h: #include <conf.h> -> inc1/conf.h
  ```

事件按深度优先先序记录、父先子后，重查在首个差异处停止，因此链上的
每一级包含者内容都已被前置事件确认一致。

批量接口：

- `query_all(tree, options_map=None)`：所有已记录入口的完整结果；
- `rebuild_set(tree, options_map=None)`：只需重编的入口及其结果。

## 用法示例

```python
from include_cache import IncludeCache, SourceTree

cache = IncludeCache()
old_tree = SourceTree({
    "src/main.c": '#include "util.h"\n#include <lib.h>\n',
    "src/util.h": "#include <conf.h>\n",
    "inc1/lib.h":  "/* lib v1 */\n",
    "inc1/conf.h": "/* conf v1 */\n",
    "inc2/conf.h": "/* conf v2 fallback */\n",
})
cache.record("src/main.c", ["-I", "inc1", "-I", "inc2"], old_tree)

new_tree = SourceTree({...})  # 另一棵源码树
result = cache.query("src/main.c", new_tree)
if result.rebuild:
    print(result.difference.kind, result.difference.path)
    for link in result.chain:
        print(" ", link)
```

## 测试状态

`test_include_cache.py` 使用内存合成数据覆盖：相同树不重编、后备文件
单独变化、靠前目录新增同名文件（含字节相同情形）、命中内容变化、嵌套
差异链、引号相对查找、循环/自包含终止、入口与选项变化、未命中后出现
候选、命中文件删除、多入口 `rebuild_set`。

**按任务约束，本测试仅作为源码交付，未在本环境执行，不声称验证通过。**
运行方式（在允许执行的环境中）：

```
python -m unittest test_include_cache -v
```
