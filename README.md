# 消息总线模式注册模块（schema_registry）

消息总线登记新读取模式时，保留期内的旧消息还会混着投递。本模块提供模式
定义、向后兼容检查与旧消息读取的本地基础设施，全部为纯 Python 源码，无
外部依赖、不联网。

## 类型系统

- `IntType` / `StringType`：整数、字符串（提供单例 `INT` / `STRING`）。
- `ArrayType(element)`：数组，元素类型可嵌套。
- `RecordType(name, fields)`：具名记录，字段可嵌套记录或数组。
- `Field(name, type, nullable=False, aliases=(), default=MISSING)`：
  - `nullable`：允许空值；
  - `aliases`：旧名，读取旧消息时的候选来源字段名；
  - `default`：缺省值，`MISSING` 表示未声明。

## 字段解析规则（兼容检查与读取共用，见 `resolution.py`）

按新字段逐层在旧记录中寻找来源字段：

1. **原名优先**：旧记录存在同名字段时直接使用。
2. **别名唯一命中才可用**：一个旧名候选项在未被占用的旧字段中恰好命中
   一个才采纳，命中多个即报歧义。
3. **同一旧字段不能分给两个新字段**：无论经原名还是别名，重复认领即冲突。

## 兼容检查（`compat.py`）

登记读取模式前，逐一比较仍在保留期的写入模式（`registry.py` 按版本顺序）：

- 类型逐层相容：int/int、string/string、记录递归、数组元素递归；
  数组元素内部的不兼容同样定位，路径以 `[]` 标记（如 `items[].sku`）。
- 旧字段可空而新字段不可空 → 不兼容：**旧消息中的显式空值须由新类型接纳**。
- 新字段在旧记录中无来源时，**缺字段才使用缺省值**，且缺省值必须类型
  正确（可空字段允许隐式 `null` 兜底）。
- 问题按字段声明顺序深度优先收集；`CompatibilityError.old_version` 是
  第一个不兼容的旧版本，`issues[0]` 是第一个不兼容路径。

## 登记与读取流程（`registry.py`）

```python
from schema_registry import SchemaRegistry, RecordType, Field, ArrayType, INT, STRING

reg = SchemaRegistry()
v1 = reg.register_writer(old_schema)          # 写入模式进入保留期
reg.register_reader("order-reader", new_schema)  # 逐一比较保留期写入模式，全部兼容才登记
out = reg.read("order-reader", v1, old_message)  # 只有登记成功才允许按同一规则读取
reg.retire_writer(v1)                          # 移出保留期，不再参与比较
```

- 读取时按同一套解析规则：原名优先、别名唯一命中、旧字段不重复分配；
  缺字段用缺省值；显式空值仅可空字段接纳，否则抛 `ReadError`。
- 未登记或登记失败的读取模式调用 `read` 会抛 `RegistryError`。

## 目录结构

```
schema_registry/
  types.py       # 类型系统：记录/数组/整数/字符串/可空字段/缺省值
  resolution.py  # 新字段到旧字段的逐层解析规则
  compat.py      # 向后兼容检查与缺省值类型校验
  reader.py      # 按同一规则读取旧消息
  registry.py    # 注册中心：保留期管理、登记前逐一比较、读取入口
tests/
  test_schema_registry.py  # 合成数据单元测试（unittest）
```

## 测试说明

`tests/test_schema_registry.py` 使用本地合成数据覆盖：原名优先、别名唯一
命中、别名歧义、旧字段重复分配、缺省值类型校验、显式空值接纳/拒绝、数组
元素内第一个不兼容路径与旧版本定位、保留期退休、未登记禁止读取等场景。

**注意：按任务要求，本次仅提供源码与测试源码，测试尚未实际运行，未做任
何验证通过的声明。** 如需运行：

```
python -m unittest discover -s tests -v
```
