# RIP/1.0 —— 科研增量补丁协议（固定子集约定）

本项目实现的是一个 **GraphQL 增量交付风格** 的固定协议子集，名称为 **RIP/1.0**
（Research Incremental Patches，科研增量补丁协议）。设计参考了 GraphQL `@defer` /
`@stream` 增量交付（incremental delivery）草案中“初始结果 + 路径补丁 + 结束标志”的
核心思想，但**不追求兼容任何外部草案**，采用本项目自己的固定示例约定。

协议版本号固定为 `RIP/1.0`，采集器拒绝解析其他版本号的消息流。

## 1. 传输与编码

- 传输方式：HTTP（`GET`，响应头 `Content-Type: application/x-ndjson; charset=utf-8`）
  或本地 NDJSON 文件，逐行传输 JSON 对象。
- 每行为一条独立消息（NDJSON），空行忽略。
- 一条消息无法解析为 JSON 对象时，采集器记录 `transport` 类决策并继续读取下一行，
  不会中断整个任务。
- 传输层异常（连接中断、文件提前结束）由采集器标记为 `interrupted`。

## 2. 路径约定

- 所有路径为 [RFC 6901 JSON Pointer](https://datatracker.ietf.org/doc/html/rfc6901)
  字符串，例如 `/authors/0/affiliation`。
- 根路径 `""` 只允许出现在 `initial` 消息中；`patch` / `insert` / `error` 使用根路径
  属于非法路径。
- 路径必须能映射到当前暂存树中的对象/数组节点；指向不存在的父节点属于非法路径。
- 数组下标必须是非负整数（`/list/0`、`/list/1` 合法，`/list/-` 非法）。
  `patch` 只能替换现存下标；`insert` 的逻辑槽位规则见 3.3。
- 非法路径不会抛异常，消息被拒绝并写入证据（`illegal_path`），后续消息继续处理。

## 3. 消息类型

### 3.1 `initial` —— 初始对象（有且仅应有一条）

```json
{"type": "initial", "seq": 0, "version": "RIP/1.0", "data": {"id": "P-1001"}}
```

- 第一条 `initial` 建立暂存树根对象；重复的 `initial` 被拒绝（`duplicate_initial`）。
- `data` 必须是 JSON 对象。

### 3.2 `patch` —— 带路径的延迟字段补丁

```json
{"type": "patch", "seq": 3, "path": "/metrics", "value": {"views": 1280}}
```

- 把 `value` 整体写入暂存树的 `path` 位置（对象键覆盖、数组下标替换）。
- 允许子补丁先于父节点到达：当 `path` 的父节点尚不存在时，消息进入**待解析队列**
  （`pending`），等父路径由后续补丁创建后，采集器按 `seq` 顺序自动重放。
- 同一 `seq` 的重复补丁只生效一次（`duplicate_seq`），证据中可看到两次到达。

### 3.3 `insert` —— 列表插入补丁

```json
{"type": "insert", "seq": 5, "path": "/references", "index": 0,
 "item": {"id": "REF-7", "title": "虚构引文七"}, "item_id": "REF-7"}
```

- 在数组 `path` 的 `index` 位置插入 `item`，`index` 为**插入时刻的逻辑槽位**
  （非负整数，等于当前数组长度表示追加）。乱序到达时历史下标可能暂时越过物理
  长度，这是允许的——采集器不做物理插入，而是把插入事件缓存到暂存区。
- 每次发布快照时，采集器把同一列表路径的全部插入按 `(index, seq)` 稳定排序，
  并以 `item_id` 去重后重建列表，因此**乱序插入、位置漂移、重复插入**都能收敛到
  唯一结果；同 `item_id` 以先到达者（`seq` 最小）为准。
- `item_id` 缺失时使用 `seq` 派生的合成 ID。

### 3.4 `error` —— 局部错误

```json
{"type": "error", "seq": 4, "path": "/funding",
 "error": {"code": "UPSTREAM_TIMEOUT", "message": "经费来源上游超时（虚构）"}}
```

- 错误绑定到具体字段路径，不影响其他字段；发布结果中该字段以
  `{"__error__": true, "code": ..., "message": ..., "source_seq": ...}` 占位，
  同时进入快照顶层 `errors` 清单，**保留错误来源（seq 与原始错误体）**。
- 错误之后若又收到同路径的正常 `patch`，占位被真实值替换、错误清单移除该项
  （错误恢复）；反之 `error` 也可覆盖已有值。
- 错误消息同样受路径合法性与待解析规则约束。

### 3.5 `end` —— 结束标志

```json
{"type": "end", "seq": 9}
```

- 收到 `end` 且不存在待解析消息时，任务状态为 `complete`。
- 收到 `end` 但仍有待解析消息时，状态为 `ended_unresolved`（流正常结束但协议违约，
  暂存树**不会**被标成完整）。
- `end` 之后到达的消息一律拒绝（`after_end`）。
- 未收到 `end`（连接中断 / 文件结束 / 传输异常）时状态为 `interrupted`，
  暂存树保持不完整，已发布快照可查看但 `complete = false`。

## 4. 采集器内部模型

采集器严格分开两份数据：

| 数据 | 作用 | 生命周期 |
| --- | --- | --- |
| **暂存树（staging）** | 接收补丁、待解析队列、插入缓冲、错误登记的可变工作区 | 任务内累积，从不直接对外发布 |
| **发布快照（published snapshot）** | 每次决策后由暂存树物化出的不可变结果，带序号与状态 | 追加保存，供页面逐步回放 |

任务重跑时新建全新的暂存树与插入缓冲；列表只由本流的 `insert` 消息重建，
因此重跑不会产生重复列表项。

## 5. 采集器声明支持的协议子集

**支持**：NDJSON over HTTP 与 NDJSON 文件两种传输；`initial` / `patch` /
`insert` / `error` / `end` 五类消息；RFC 6901 路径；子先于父待解析与重放；
列表乱序插入、按 `(index, seq)` 排序与 `item_id` 去重；同 `seq` 去重；
局部错误、错误恢复与错误来源留存；`complete` / `ended_unresolved` /
`interrupted` 三种结束判定。

**不支持（明确排除）**：GraphQL 官方响应封装与 `hasNext` 字段、多入口
`incremental` 数组、`subPath`/自定义路径表示、非 NDJSON 多部分（multipart）传输、
`initial` 以外的根写操作、删除/移动类补丁、任何需要真实账户或外部业务服务的鉴权。
