# 网关路由表（gateway_routing）

按 HTTP 方法登记路径模板，发布前做**精确的路径集合冲突检查**：两个模板只有在一个语言严格包含另一个时才允许共存；真正匹配请求时交给**路径集合最小**的那条规则。整批校验失败则原路由表保持不变，并报告冲突规则与一条两者都能命中的真实路径。

仅使用 Python 3.11 标准库，无第三方依赖，不访问网络。

## 路径模板语法

模板以 `/` 开头，按斜线分段，**不接受**空段（因此不接受尾随 `/`、`//`）：

| 片段 | 含义 |
| --- | --- |
| `a`、`health`、`v1` | 非空字面量，精确匹配该段 |
| `{name}` | 单段参数，匹配任意**非空**段，并捕获该段原值 |
| `**` | 仅允许作为最后一段，匹配**零到多**段 |

- `"/"` 是根路径，对应零个片段。
- `**` 只能整段出现；`*`、`b**c` 之类都是普通字面量。
- 参数名不能为空、不能含花括号/空白/`*`；`{a}b`、`x{y}`、`/a/**/b` 均非法。
- 同一模板里参数重名时，捕获结果以最后一次出现为准。

## 冲突判定（精确，非近似）

设两条规则的匹配集合为 `L(A)`、`L(B)`：

| 关系 | 能否共存 |
| --- | --- |
| 不相交 | 可以 |
| `L(A) ⊂ L(B)` 或 `L(B) ⊂ L(A)`（**严格**包含） | 可以 |
| 集合相等 | **冲突** |
| 相交但互不包含 | **冲突** |

因此 `/a/{x}` 与 `/{y}/b` 是冲突的（字面量段数相同、无法靠“字面量更多”排序），报告的交集路径为 `/a/b`。判定逻辑直接来自语法结构（固定前缀 + 可选末尾 `**`），对本题语法是精确的，不依赖采样。

请求分派：在全部能命中的规则中取路径集合最小者。可发布的路由表在集合包含关系下构成链，所以最小命中规则唯一；这与登记顺序无关。

## 公开 API

```python
from gateway_routing import (
    Router, Route, RouteTable, RouteMatch, Conflict,
    Template, TemplatePair,
    parse_template, split_path, analyze_templates,
    RoutingError, TemplateError, PathError, PublishError, RouteConflictError,
)
```

### `Router`

| 方法 | 说明 |
| --- | --- |
| `publish(method, routes) -> RouteTable` | 原子替换某个方法的路由；`routes` 可为 `Route`/模板字符串的列表，也可传单个字符串 |
| `publish_batch({method: routes, ...}) -> dict[str, RouteTable]` | 多方法整批原子发布；任一方法有冲突则全部不生效 |
| `replace_all({method: routes, ...})` | 原子替换整个路由表，未出现的方法被移除 |
| `check(method, routes) -> tuple[Conflict, ...]` | 试运行：只返回冲突，不修改任何状态 |
| `resolve(method, path) -> RouteMatch \| None` | 未知方法或非法路径返回 `None` |
| `table(method) -> RouteTable` | 当前不可变快照（含 `match`） |
| `routes(method)` / `methods()` / `snapshot()` / `len(router)` | 只读查询 |

HTTP 方法会被 `strip().upper()` 规范化；空方法名抛 `RoutingError`。

### `Route` / `RouteMatch` / `Conflict`

- `Route.create(template, payload=None, name=None)`：模板 + 任意载荷（例如 handler）+ 可选名称。
- `RouteMatch`：`route`、`params`（只读映射）、`path`、`segments`。
- `Conflict`：`method`、`a`、`b`、`pair`，以及 `witness_path`（两者都能命中的一条实际路径）与 `describe()`。

### 模板工具

- `parse_template(text) -> Template`：语法校验；非法时抛 `TemplateError`。
- `split_path(path) -> tuple[str, ...]`：`"/"` → `()`；非法路径抛 `PathError`。
- `Template.match_path(path)` / `match_segments(segments)`：命中返回参数字典（含 `{}`），否则 `None`。
- `analyze_templates(a, b) -> TemplatePair`：精确给出 `intersects`、`witness_path`、`a_subset_b`、`b_subset_a`、`equal`、`kind`（`disjoint`/`equal`/`a_subset_b`/`b_subset_a`/`overlap`）、`conflict`。

### `RouteConflictError`

`PublishError` 的子类。携带 `conflicts`（全部冲突对）、`methods`、`for_method(method)`；`str(err)` 逐条列出方法、两条规则和交集路径。发布被拒时旧表（含同一批中本可发布的方法）保持原样。

## 使用示例

```python
from gateway_routing import Router, RouteConflictError

router = Router()
router.publish("GET", ["/**", "/health", "/teams/{team}/members", "/teams/{team}/**"])

m = router.resolve("GET", "/teams/red/members")
assert m.route.template.text == "/teams/{team}/members"
assert m.params == {"team": "red"}
assert router.resolve("GET", "/teams/red/deep/x").route.template.text == "/teams/{team}/**"
assert router.resolve("GET", "/health").route.template.text == "/health"

try:
    router.publish("GET", ["/a/{x}", "/{y}/b"])       # 冲突
except RouteConflictError as caught:
    print(caught.conflicts[0].witness_path)              # /a/b
    print(router.resolve("GET", "/health") is not None)  # True：旧表仍在
```

## 复现与验证

环境：题目规定的 Python 3.11.6，工作区根目录，无第三方依赖。

```bash
# 全量测试（从工作区根目录自动发现 tests/test_*.py）
python3 -I -m unittest discover -v

# 指定起点等价写法
python3 -I -m unittest discover -s tests -t . -v
```

`-I` 为隔离模式，避免当前目录/环境变量中的同名模块干扰。

### 测试文件与覆盖范围

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_templates.py` | 模板/路径解析的合法与非法边界（根、尾斜杠、空段、`**` 位置、花括号、参数名、非字符串）；匹配与捕获；`analyze_templates` 的包含/相等/相交/不相交判定；**穷举对拍**：对 3 段以内、片段取自 `a/b/{x}/{y}` 且可带末尾 `**` 的全部模板两两组合，用长度 0–4 的路径全集做独立判定，逐对核对 `intersects`/`a_subset_b`/`b_subset_a`/`equal`，并验证每条 witness 确实被两个模板命中 |
| `tests/test_router.py` | 发布/替换/多方法隔离；冲突拒绝与消息内容（含 witness）；相等集合、重叠、不相交；**原子性**（首次发布失败、替换失败、跨方法整批失败均保持旧快照）；优先级（更具体胜出、顺序无关、`/**` 与有限规则共存）；根路径、Unicode/编码片段、重复参数；`RouteTable` 直接构造校验 |
| `tests/test_scenarios.py` | 题面示例 `/a/{x}` vs `/{y}/b` → 冲突且交集为 `/a/b`；多团队真实路由表一次性发布后每条路径的命中结果；对穷举探测路径断言“胜者集合 ⊆ 每个候选集合” |

验证范围与已知边界：

- 已验证：上述语法内的解析、精确集合关系、冲突拒绝与原子回滚、最小集合分派、错误类型与消息。
- 未涉及（超出题面语法）：模板内 `**` 出现在非末尾、正则/通配混用、URL 解码、查询串、大小写不敏感路径匹配；宿主 HTTP 服务器集成。
- 所有测试均为真实执行结果；本 README 不含预置的“演示输出”。

## 目录结构

```
gateway_routing/
  __init__.py     公开 API 导出
  errors.py       异常层次
  templates.py    模板解析、匹配、集合关系与 witness
  router.py       Route / RouteTable / Router，原子发布与分派
tests/
  __init__.py
  test_templates.py
  test_router.py
  test_scenarios.py
```
