# release_check — 发布检查器

一个静态检查器：给定**有限的调用者身份集合**、**接口继承图**（每个接口逐方法声明
"哪种身份最多能看到哪些字段"）以及**若干候选实现**（声明它接受哪些身份、实际可能返回
哪些字段），判断每个候选实现能否作为替换实现发布。

## 需求映射

题面中的交叉约束在实现里对应如下：

| 题面要求 | 实现位置 |
| --- | --- |
| 读入有限调用者集合、继承图、方法声明 | `release_check/model.py` 的 `parse_spec` |
| 每个声明列出允许调用的身份及每种身份最多可见的字段 | `Interface.declarations` |
| 实现列出接受的身份与实际可能返回的字段 | `ImplMethod.accepts` / `return_fields` |
| 沿公开接口合法的调用在替换后仍须成功 | `missing-method` / `rejects-identity` 检查 |
| 返回值落在该接口给该身份的可见范围内 | `overexposure` 检查 |
| 菱形继承里同一实现同时受多份契约约束 | `Spec._compute_contract` 对分支取交集 |
| 输出精确到入口和身份 | 每个 `Finding` 都带 `entry`（`接口.方法`）与 `identity` |
| 缺少公开父接口的声明时保留未决 | `Resolution.pending` → `PendingEntry`，状态 `pending` |

## 语义要点

**入口（entry）** 指一对 `(接口, 方法)`。检查针对"外部调用者可能持有的引用"进行：
只有 `public: true` 的接口（以及它们的公开祖先）才构成调用入口；私有接口不产生入口。

**声明即上限。** 一个身份允许看到 `[id, plan]` 意味着实现返回 `[id]` 也合法——结果仍
落在可见范围内。若需要"每个被授予的字段都必须返回"的更严格语义，使用 `--exact`，它会
额外报出 `reduced-visibility`。

**菱形继承。** 子接口自己声明了该方法时，声明**覆盖**继承来的契约。没有覆盖时，所有
祖先分支的契约**同时生效**：某身份只有在每个分支都授予它时才可调用，可见字段取各分支
的交集。因此同一实现会同时被两条路径约束，报告会给出 `origins`（产生该限制的接口）。

**未决状态。** 若链路上存在一个公开接口通过 `exposes` 声明"这个方法在对外接口上，但
可见性未声明"，该入口不会被猜测为通过或拒绝，而是进入 `pending`。整体状态优先级为
`rejected > pending > ready`，`ok` 仅在 `ready` 时为真。

**严格解析。** 未知键、悬空的 `extends`/`implements`、重复名字、重复字段、未在调用者
集合中的身份、继承环都会抛 `InputError`，避免笔误悄悄让某项检查失效。

## 入口

模块入口为 `release_check.cli:main`，也可用 `python -m release_check`。

```bash
# 全量检查（文本）
.venv/bin/python -m release_check tests/fixtures/diamond.json

# 机器可读
.venv/bin/python -m release_check tests/fixtures/diamond.json --format json

# 只问"能否用某实现替换某公开入口"
.venv/bin/python -m release_check tests/fixtures/diamond.json \
    --substitute IDiamondProfile SafeProfileGenerator

# 严格模式：被授予的字段必须全部返回
.venv/bin/python -m release_check tests/fixtures/diamond.json --exact

# 从标准输入读取
cat spec.json | .venv/bin/python -m release_check -
```

退出码：

| 码 | 含义 |
| --- | --- |
| `0` | 所有入口可判定且无违规（`ready`） |
| `1` | 至少一条违规（`rejected`） |
| `2` | 无法完全判定：仍有未决入口，或规格本身不合法 |

Python 库入口：

```python
from release_check import parse_spec, check, render_text

spec = parse_spec(document)
report = check(spec)
print(render_text(report))
report.status   # "ready" | "pending" | "rejected"
```

## 规格格式

```json
{
  "callers": ["billing", "analytics", "support", "auditor"],
  "interfaces": [
    {
      "name": "IProfileSource",
      "public": true,
      "extends": [],
      "exposes": [],
      "methods": {
        "generate": {
          "billing": ["id", "email", "plan"],
          "analytics": ["id", "plan"]
        }
      }
    }
  ],
  "implementations": [
    {
      "name": "SafeProfileGenerator",
      "implements": ["IDiamondProfile"],
      "methods": {
        "generate": {
          "accepts": ["billing", "analytics"],
          "returns": {"billing": ["id", "plan"], "analytics": ["id"]}
        }
      }
    }
  ]
}
```

`returns` 既可以是按身份映射的对象，也可以是适用于所有被接受身份的一个字段列表。
`public` 缺省为 `true`。`exposes` 用于标记"属于对外接口但可见性声明缺失"的方法。

## 验证范围

测试位于 `tests/`，使用标准库 `unittest`，全部为真实断言（异常捕获 + 精确比对报告
内容），并要求通过真实子进程驱动 CLI 以校验退出码与序列化输出。

```bash
.venv/bin/python run_tests.py          # 推荐入口，68 个用例
.venv/bin/python run_tests.py -v       # 逐条显示
.venv/bin/python run_tests.py test_reject   # 只跑某个模块
```

也可以直接用 unittest 发现：

```bash
.venv/bin/python -m unittest discover -s tests -t tests -v
```

`run_tests.py` 会把仓库根目录与 `tests/` 放入 `sys.path`，因此在任意工作目录、以及
`-I`/`-P` 隔离模式下都能运行；退出码为 `0` 仅当全部断言通过。

覆盖内容：

- `tests/test_accept.py` — 可接受结果：字段完全匹配、返回子集、扁平 `returns`、
  接受入口未准入的身份、菱形交集内合规、替换检查通过、`--exact` 下全覆盖。
- `tests/test_reject.py` — 应被阻止的结果：字段越界、拒绝已准入身份、缺少方法、
  按身份分别判定、菱形冲突报告 `origins`、替换泄漏、替换拒绝调用者。
- `tests/test_boundaries.py` — 影响两者的边界/交错场景：覆盖 vs 继承、父接口更窄/
  更宽、兄弟分支冲突与被私有接口消解、不相交身份集合、四级深链传播、未决入口不被
  判错、空可见性映射（"谁都不准入"不等于"无约束"）、多实现交错排序、单实现绑定到
  两个接口、`--exact` 不重复计违规。
- `tests/test_validation.py` — 规格校验：未知键、悬空父接口、重复名/字段、未知身份、
  继承环、`returns` 指向未接受身份、`exposes` 与声明冲突等。
- `tests/test_cli.py` — 端到端：退出码 0/1/2、JSON 结构、stdin、`--substitute`、
  未知目标报错、输出确定性。

共享构造器在 `tests/support.py`；端到端夹具为 `tests/fixtures/diamond.json`（一个
真实菱形：`IDiamondProfile` 合并读路径与审计路径，两条分支对 `billing` 的可见字段
不同，用于验证交集与 `origins`）。

## 已知限制

- 仅做静态判定：不执行、不加载实现代码，所有"实际返回"以规格中的声明为准。
- 方法之间没有顺序或副作用建模；每个 `(入口, 身份)` 独立判定。
- `--exact` 是额外语义，题面本身只要求"落在可见范围内"，默认行为对应题面。
