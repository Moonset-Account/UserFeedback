# 反序列化字段策略兼容核查组件（compat_audit）

评审反序列化配置时，回答同一个业务问题：**同一份业务输入，在新旧读写端（生产者/消费者）
上会落到哪种字段处理策略，二者在重叠的协议升级窗口内是否兼容。**

组件读取三类输入——配置层（含优先级）、端点支持的协议版本范围、各消息类型允许/强制的
字段策略——逐对输出兼容结论，并在阻断时定位到实际生效的配置层条目。

## 目录结构

```
compat_audit/            核查组件包
  model.py               领域模型、枚举与报告结构
  config.py              JSON 配置加载与结构校验（三态语义入口）
  resolver.py            生效选项解析（层优先级 / 作用域 / 三级回退）
  audit.py               核查引擎，逐对生产者×消费者产出结论
  cli.py                 命令行入口
  __main__.py            支持 python -m compat_audit
fixtures/sample_config.json  本地合成夹具（无外部数据）
tests/                   unittest 验收用例（5 个模块，47 个用例）
run_audit.py             CLI 运行器（隔离模式 -I 可用）
run_tests.py             测试运行器（隔离模式 -I 可用）
```

## 实际入口

核查命令（在工作区根目录执行；Python 3.11.6）：

```bash
# 推荐：隔离模式，防止夹具目录中的同名模块污染导入
python3 -I run_audit.py fixtures/sample_config.json
python3 -I run_audit.py fixtures/sample_config.json --report out/report.json

# 非隔离模式下也可直接用模块入口
python3 -m compat_audit fixtures/sample_config.json
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部组合 `compatible` 或 `no_overlap`，无阻断 |
| 2 | 存在 `blocked` 组合（策略冲突，报告含原因与生效配置层） |
| 1 | 配置文件缺失或结构非法（错误信息输出到 stderr） |

测试命令：

```bash
python3 -I run_tests.py          # 隔离模式运行全部 47 个用例
python3 -m unittest discover -s tests   # 普通模式等价入口
```

> 说明：Python 隔离模式（`-I`）不会把当前目录加入 `sys.path`，因此隔离模式下请使用
> `run_audit.py` / `run_tests.py` 两个根目录运行器；它们只做路径注入，不含业务逻辑。

也可作为库调用：

```python
from compat_audit import load_config, audit_config
report = audit_config(load_config("fixtures/sample_config.json"))
for conclusion in report.conclusions:
    print(conclusion.pair_id, conclusion.status)
```

## 配置语义

### 字段策略

* `unknown_fields`（枚举）：未声明字段的处理方式——`preserve`（保留）/ `reject`（拒绝）/
  `drop`（丢弃）。
* `extra_validation`（布尔）：额外校验保护项是否开启。

### 三态区分（未定义 ≠ 显式禁用）

* 键缺失或值为 JSON `null` → **未定义**，不进入配置层条目，继续向下回退；
* 布尔值 `false` → **显式禁用**，保留在条目中，报告里 `explicitly_disabled=true` 并归因
  到具体配置层；
* 其它显式值 → 正常配置，优先于版本默认值。

### 生效值解析顺序

1. 端点配置层条目，排序键依次为：配置层 `priority`（大者优先）→ 作用域精确度
   （精确版本/精确消息类型优先于通配）→ 同层同作用域时条目在配置中的顺序（后者优先）；
2. `version_defaults` 中该协议版本的默认值；
3. 内建兜底（`unknown_fields=preserve`、`extra_validation=false`），此时
   `defined=false`，明确标记“选项未定义”。

### 兼容结论（PairStatus）

* `compatible`：版本范围有交集，且交集内每个（版本 × 消息类型 × 端点）单元均通过；
* `blocked`：重叠升级窗口内至少一个单元冲突。任一读写端把“本应拒绝的未声明字段”
  实际置为 `preserve` 即阻断（原因码 `undeclared_fields_retained`），报告给出
  生效值来源：配置层名、层优先级、版本/消息类型作用域、条目序号；
* `no_overlap`：双方协议版本范围无交集，不存在共同升级窗口，不做策略评估。

其余阻断原因码：`policy_not_allowed`（生效策略不在消息类型允许集合内，或强制策略未被
满足但未保留字段）、`safeguard_explicitly_disabled`（保护项被配置层显式关闭）、
`safeguard_undefined`（保护项未定义，经版本默认/内建兜底得到关闭值）。

## 验证范围（tests/，全部为真实断言）

| 测试模块 | 覆盖内容 |
| --- | --- |
| `test_audit_fixture.py` | 合成夹具端到端：可接受组合通过；preserve 命中强制 reject 时阻断并定位 service 层；未定义走内建兜底仍阻断；drop 冲突定位；显式禁用 vs 未定义分开归因；无交集组合不评估 |
| `test_resolver.py` | 层优先级压过作用域精确度、同层精确作用域优先、同作用域后条目优先、不匹配作用域忽略；显式层值覆盖版本默认、版本默认回退、内建兜底、null 视为未定义；高优先级 preserve 的端到端阻断 |
| `test_tri_state.py` | 三态专项：显式禁用（带层定位）、完全未定义（`defined=false`）、键存在但为 null、版本默认给 false（已定义但非显式层禁用）、显式启用通过、未定义在宽松消息类型上可放行 |
| `test_version_window.py` | 边界与交错：单点版本相接、相接点阻断、范围完全错开、全范围逐版本评估、窗口内部分单元阻断则整对阻断、版本顺序按 `order` 而非字典序 |
| `test_config_validation.py` | 拒绝路径：重复版本/组合、未定义配置层引用、非法枚举值、布尔类型错误、未知选项键、倒置版本范围、强制策略不在允许集合、角色不匹配、全 null 条目被剔除、缺失文件 |
| `test_cli.py` | 进程内退出码（0/1/2）与报告文件；子进程方式验证 `python -I run_audit.py` 与 `python -m compat_audit` 两个真实入口 |

验收要求对应关系：

* **至少一个可接受结果**：`test_acceptable_pair_is_compatible` 等；
* **一个应被阻止/拒绝的结果**：`test_blocked_pair_locates_effective_layer_for_retained_fields`
  及全部 `blocked` / `ConfigError` 用例；
* **影响两者的边界或交错场景**：`test_version_window.py` 全部用例 +
  `test_resolver.py` 的优先级交错用例 + 三态回退链用例。

## 范围与安全说明

* 本组件只做静态配置核查：输入为本地 JSON 数据，不进行任何网络访问、目标探测或消息处理；
* 夹具为本地合成数据，不包含真实业务凭据；
* 不读取用户账号配置或环境凭据；测试可在 `python -I` 隔离模式下完整运行。
