# 支持的协议子集声明（Protocol Subset）

本项目的采集请求构造器**不是**通用 OpenAPI 客户端，仅实现下列显式声明的组合。
任何落在子集之外的输入都会在**发出 HTTP 请求之前**抛出
`UnsupportedCombinationError` / `ValidationError`，不会静默降级
（例如不会偷偷把数组改成逗号拼接）。

## 1. 文档与方法

| 维度 | 支持范围 |
|---|---|
| 规范版本 | OpenAPI **3.0.x**（3.1 不保证） |
| HTTP 方法 | **仅 GET**；文档出现 POST/PUT/PATCH/DELETE 等写操作时直接拒绝加载 |
| 参数位置 | 仅 `in: query` 与 `in: path`；`header` / `cookie` 参数拒绝 |
| 请求体 | 不允许 `requestBody` |
| 数据类型 | `string` / `integer` / `boolean`；`array`（元素限标量）；`object`（限一层标量属性） |
| 嵌套 | 数组元素为对象、多层对象、对象数组等一律拒绝 |

## 2. query 参数序列化矩阵

| style | explode | 标量 | 标量数组 | 一层对象 | 示例 |
|---|---|---|---|---|---|
| `form` | `true`（默认） | ✅ | ✅ 重复键 `tags=a&tags=b` | ✅ `field=x&order=asc` | — |
| `form` | `false` | ✅ | ✅ 逗号拼接，值内逗号编码为 `%2C` | ✅ `sort=field,x,order,asc` | `codes=TB-21%2C7,MB%2F330` |
| `spaceDelimited` | 固定 `false` | ❌ | ✅ `scores=92%2095` | ❌ | — |
| `pipeDelimited` | 固定 `false` | ❌ | ✅ `levels=A\|C` | ❌ | — |
| `deepObject` | 固定 `false` | ❌ | ❌ | ✅ 仅一层标量对象 | `filter%5Bstatus%5D=running` |

## 3. path 参数序列化矩阵

| style | explode | 标量 | 数组 | 一层对象 |
|---|---|---|---|---|
| `simple` | `false` / `true` | ✅ `EXP-001` | `a,b` / `a,b`（simple 无 explode 差异分隔符，对象有差异） | `k,v,n,1` / `k=v,n=1` |
| `label` | `false` / `true` | ✅ `.v` | `.a,b` / `.a.b` | `.k,v,n,1` / `.k=v.n=1` |
| `matrix` | `false` / `true` | ✅ `;id=v` | `;id=a,b` / `;id=a;id=b` | `;o=k,v,n,1` / `;k=v;n=1` |

path 中所有保留字符（含 `,` `/` `;` `=`）在**值内部**均按 RFC 3986 百分号编码，
例如 `EXP/002,next` → `EXP%2F002%2Cnext`，避免破坏路径结构。

## 4. 字符编码规则

* 默认严格模式：仅 RFC 3986 unreserved 集合 `A-Z a-z 0-9 - _ . ~` 原样，其余一律
  UTF-8 字节百分号编码，十六进制大写（`林` → `%E6%9E%97`）。
* `allowReserved: true`（仅 query 标量）：值中的
  `, / ? : @ ! $ & ' ( ) * + ;` 保持原样；但**结构分隔符 `= # %` 始终编码**
  （`a/b,c:d=e#f` → `a/b,c:d%3De%23f`）。
* 方括号 `[ ]` 属于结构字符，deepObject 中编码为 `%5B` / `%5D`。
* 空格在 query 中编码为 `%20`（不使用 `+`）。

## 5. 空值约定

| 情形 | 行为 |
|---|---|
| query 空数组 `[]` / 空对象 `{}` | 从 query string 中**省略**，并在 `omitted` 与参数计划中留痕 |
| query 空字符串标量 | 视为“不传”，同样省略并留痕 |
| path 空数组 / 空对象 | **拒绝**（路径片段无法省略） |
| 缺必填参数 | 拒绝（`ValidationError`） |

## 6. 明确拒绝的典型组合（验收场景覆盖）

1. 给 `deepObject` 参数传数组或多层嵌套对象；
2. 发送文档未声明的参数（不允许参数注入）；
3. 类型不匹配（integer 传字符串、boolean 传 `"yes"` 等，不做隐式转换）；
4. query 用 `simple/label/matrix`，或 path 用 `form`；
5. path 参数空数组/空对象；
6. 文档自身含写操作、cookie/header 参数、requestBody 或深层嵌套 schema。
