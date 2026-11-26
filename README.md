# UID-17195 脱敏网关处理模块

同一检查的**影像、标注、复查清单**分三次导出时，脱敏网关需要：

1. 接收 **JSON 表示的 DICOM 属性树**和**字段字典**，**递归处理序列项（SQ）**；
2. 对类型为 **UID（DICOM VR=`UI`，字典中也接受写作 `UID`）** 的字段：
   - 实例标识（Study/Series/SOP/Frame of Reference 等）及其引用
     （`ReferencedSOPInstanceUID` 等）统一换成**按接收项目隔离的合法数字点串**；
   - 表示编码方式的**登记常量**（Transfer Syntax UID 及字典登记的常量）**原样保留**；
3. **字典未批准的私有属性整体移除**（连同其内容，递归到任意深度的序列项内）；
4. **标注先于影像到达**时，两批/三批中的同一原始 UID 仍映射到同一假名，互相对得上；
5. 映射可落盘，**新进程载入映射文件后结果不变**；
6. **并发导出共用同一映射**（线程锁 + 文件咨询锁 + 原子替换）；
7. 可向短编号生成器**注入碰撞**：已外发的号码绝不改绑给另一实例，碰撞候选被跳过并重新分配。

仅使用 Python 3.11 标准库，无第三方依赖、无网络访问；所有验证数据均为本地合成数据。

---

## 目录结构

```
deid_gateway/
  __init__.py        # 公开 API 导出
  uids.py            # UID 合法性、短编号生成器、UidMapper（持久化/隔离/碰撞/锁）
  dictionary.py      # FieldDictionary、私有标记与批准、传输语法常量表
  processor.py       # JSON 属性树递归转换、序列递归、私有属性移除、审计报告
  gateway.py         # DeidGateway 门面、deidentify() 一次性函数、ExportResult
  cli.py / __main__.py  # python3 -m deid_gateway 命令行
gateway.py           # 兼容垫片：import gateway
uid_mapper.py        # 兼容垫片：import uid_mapper
mapper.py / deid.py  # 兼容垫片：import mapper / import deid
tests/               # 63 个 unittest 用例（7 个 test_*.py + 多进程 worker + fixtures）
examples/
  end_to_end_demo.py # 合成三批数据的端到端可执行演示
```

---

## 运行方法

在工作区根目录执行题目要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

只跑某个文件/用例：

```bash
python3 -m unittest tests.test_uids -v
python3 tests/test_concurrency.py          # 也可直接运行
```

端到端合成演示（标注先到 → 新进程载入 → 影像/清单对齐 → 碰撞注入 → 并发）：

```bash
python3 examples/end_to_end_demo.py
```

命令行处理一个导出文件（真实新进程持久化入口）：

```bash
python3 -m deid_gateway \
  --dictionary tests/fixtures/field_dictionary.json \
  --mapping /tmp/deid-uid-map.json \
  --project hospital-a \
  --input tests/fixtures/annotation_export.json \
  --output /tmp/annotation.out.json \
  --report
```

---

## 公开 API

### `DeidGateway(dictionary=None, *, mapping_path=None, project=None, base_root=..., mapper=None, code_generator=None, preserved_uids=None, name_heuristics=True)`

门面类。`dictionary` 可以是 `FieldDictionary`、普通 `dict`/`list`、JSON 字符串或字典文件路径。
指定 `mapping_path` 后，每次产生新映射都会在文件事务（`fcntl` 锁 + 临时文件 `os.replace`
原子替换）中落盘；不传则为纯内存映射。

常用方法：

| 方法 | 说明 |
| --- | --- |
| `process(tree, project=None, *, report=None)` | 返回脱敏后的**新树**（不修改入参）；`tree` 可为 dict/list 或 JSON 文本 |
| `process_json(payload, project=None)` | 输入/输出均为 JSON 文本 |
| `export(tree, project=None, *, batch=None)` | 返回 `ExportResult(tree, report, batch, project, mapper)` |
| `process_file(in, out, project)` | 读 JSON 文件、处理、可选写出 |
| `map_uid(uid, project=None)` | 直接映射单个 UID |
| `save_mapping(path=None)` | 原子保存映射 |
| `DeidGateway.load_mapping(path, dictionary=...)` | 从映射文件恢复网关 |

一次性函数：`deidentify(tree, dictionary=None, *, project=None, ...)`；
函数式入口：`process_tree(tree, dictionary=None, mapper=None, *, project=None, report=None)`
（别名 `process_attribute_tree` / `deidentify_tree` / `process_json_tree`）。

### `UidMapper(mapping_path=None, *, base_root=..., code_generator=None, preserved_uids=None, max_attempts=32)`

项目隔离、可持久化、碰撞安全的映射表（别名 `UidMapping` / `UidMappingStore` / `MappingStore` / `UidRegistry`）。

- `map_uid(source_uid, project=None) -> str`：常量原样返回；空值透传；否则返回该项目下稳定唯一的合法 UID。
- `bind(source, pseudo, project=None)`：显式登记既有映射；同对幂等；**任何改绑已外发号码的行为抛 `MappingConflictError`**。
- `resolve(pseudo, project=None)` / `lookup(source, project=None)` / `mapping_for(project)`。
- `save(path=None)` / `UidMapper.load(path)`。
- 属性 `collision_events`：记录每次候选碰撞/回退，便于审计与测试断言。

### 短编号生成器与碰撞注入

```python
from deid_gateway import UidMapper, ScriptedShortCodeGenerator

# 先脚本化返回两个 7（制造碰撞），再返回 8
gen = ScriptedShortCodeGenerator(["7", "7", "8"])
m = UidMapper(base_root="1.2.3.4", code_generator=gen)
a = m.map_uid("A", "p")     # 1.2.3.4.1.7
b = m.map_uid("B", "p")     # 候选 .7 撞号 -> 重试得到 1.2.3.4.1.8
assert m.map_uid("A", "p") == a
assert m.resolve("1.2.3.4.1.7", "p") == "A"
```

- `ShortCodeGenerator(start=1)`：按命名空间单调递增，可继承并重写 `next_code(namespace)` 注入故障。
- `ScriptedShortCodeGenerator.collide_once(code)` / `.always(code)`：一次性/持续碰撞注入。
- 持续碰撞达到尝试上限后，使用持久化单调弧 `… .99.<n>`（过长时退到 `2.25.<128 位整数>`），
  并仍在反向表中查重，保证两个实例绝不共享号码。

### `FieldDictionary.from_dict(data)` / `FieldDictionary.load(path)`

接受的常见字典形状：

```json
{
  "fields": {
    "0020000D": {"keyword": "StudyInstanceUID", "vr": "UID", "role": "id"},
    "TransferSyntaxUID": {"tag": "00020010", "vr": "UI", "role": "encoding_constant"}
  },
  "approved_private": ["00191020", "ApprovedSiteCode"],
  "registered_constants": ["1.2.840.10008.5.1.4.1.1.2"]
}
```

也接受：`{关键字: "UID"}` 扁平映射、`fields` 为列表、`type`/`data_type` 代替 `vr`、
`"SEQUENCE"` 代替 `"SQ"` 等。tag 支持 `0020000D`、`0020,000D`、`(0020,000D)` 等写法；
关键字匹配忽略大小写/下划线/连字符（`studyInstanceUID` 与 `StudyInstanceUID` 等价）。

### `ProcessReport`

`report.replaced` / `constants_kept` / `private_removed` / `approved_private_kept`
为 JSON 路径列表；`counts()` / `as_dict()` 输出统计与事件明细。

### UID 合法性

`is_legal_uid(s)` 按 DICOM UID 文本规则校验：仅数字与 `.`、长度 ≤ 64、每段为
`0` 或不带前导零的正整数、不得有空段。所有由映射器发出的串都经 `assert_legal_uid` 校验。

---

## 关键语义说明

### 假名结构与项目隔离

默认假名形如：

```
<base_root>.<项目编号>.<项目内短编号>
例：1.2.840.999999.1.7
```

项目名到数字项目编号的登记也持久化在映射文件中。同一原始 UID 在不同接收项目下得到
不同假名与不同反向命名空间；项目内则严格稳定。

### 哪些 UID 保持原样

按优先级：

1. 字典中 `role` 属于 `constant/encoding_constant/transfer_syntax/preserve/...` 的字段；
2. 字段条目 `constants: [...]` 列出的具体值；
3. 网关/字典的 `registered_constants`（含内置常见 Transfer Syntax 登记表
   `deid_gateway.TRANSFER_SYNTAX_UIDS`）。

其余所有 UID 类型值（标识与引用）都过同一映射表，因此“标识—引用”天然一致。
注意：默认策略只自动保留**传输语法/编码登记常量**；SOP Class 等可在字典中声明为
`constant`（fixture 即如此示范），或加入 `registered_constants`。

### 私有属性判定

- tag 形式键且 group 为奇数（DICOM 私有段规则），或字典条目标 `"private": true`；
- 只有条目 `"approved": true`、或出现在 `approved_private` 名单中的才保留；
- 未批准者键与值整体删除，删除动作递归作用于任意深度的 SQ / `items` / `children`。

### 支持的属性树形状

- 标准 DICOM JSON：`{"0020000D": {"vr":"UI","Value":["..."]}}`，SQ 的 `Value` 为对象数组；
- 关键字简化值：`{"StudyInstanceUID": "...", "Seq": [{"Ref...UID": "..."}]}`；
- 显式节点：`{"tag":"...", "type":"UID", "value":"..."}` 以及 `items/children` 容器；
- 复查清单这类普通 JSON：凡键名是 DICOM 风格 `...UID`（不会误伤 `guid`/`GUID`）
  或字典已登记类型的字段都会处理，其余 JSON 原样保留。

### 持久化文件格式

```json
{
  "format": "deid-gateway-uid-map/1",
  "base_root": "1.2.840.999999.1",
  "projects": {"hospital-a": "1"},
  "project_seq": 1,
  "fallback_seq": {"hospital-a": 0},
  "forward": {"hospital-a": {"原始UID": "假名UID"}}
}
```

反向表由 `forward` 重建；载入时若发现同一假名绑了两个来源会抛 `MappingConflictError`。

### 并发模型

- 进程内：`threading.RLock` 串行化“查表—分配—登记”；
- 使用映射文件时：对 `<mapping>.lock` 加 `fcntl.LOCK_EX`，取锁后重新载入磁盘最新状态，
  提交时临时文件 + `os.replace` 原子替换；
- 因此多线程、多进程并发导出可共用同一映射（测试含 4 个真实子进程同时写同一映射）。

---

## 验证范围（本地实测）

最终一次 `python3 -m unittest discover -s tests -v` 实测：**7 个测试文件、63 个用例全部通过**
（`Ran 63 tests ... OK`），并连续重复 3 轮无抖动；在 `python3 -I` 隔离模式下同样通过。

| 测试文件 | 用例数 | 覆盖的题面要求 / 边界与交错 |
| --- | ---: | --- |
| `tests/test_uids.py` | 16 | UID 合法/非法边界（长度 64、前导零、空段、非数字）；短编号生成器与碰撞脚本；项目隔离；碰撞注入后已外发号码不改绑、来源稳定性、反向归属；持续碰撞回退；`bind` 双向冲突；持久化往返；超长 root 仍产出合法 UID；假名二次处理幂等；8 线程 1600 条双射 |
| `tests/test_dictionary_privacy.py` | 10 | tag 规范化、奇组私有判定；多种字典形状；批准名单；未批准私有属性及内容删除；序列深层递归删除；显式私有关键字；输入树不被修改 |
| `tests/test_processor.py` | 15 | 标准 DICOM JSON、VM 多值、空值/缺值；标识与引用同号；传输语法/字典常量/登记值原样保留且不入映射；简化/驼峰/显式节点形状；`guid`/`GUID` 不误伤；JSON 文本 I/O；审计报告；重复处理幂等 |
| `tests/test_batches_interleaving.py` | 4 | 合成影像/标注/复查清单三批数据；**标注先到**时 Study/Series/SOP/Frame 与各级引用三批对齐；影像先到对照；中途用新 `UidMapper.load` 重建网关结果不变；跨项目隔离 |
| `tests/test_persistence_process.py` | 2 | **真实启动新的 OS 进程**运行 `python -m deid_gateway`：进程 1 处理标注，进程 2 载入映射后输出逐字节相同；进程 3/4 影像与清单对齐；另一项目隔离；原始 UID 不泄漏到产出文件 |
| `tests/test_concurrency.py` | 3 | 12 线程并发处理文件映射（双射、共享 UID 全线程同号、落盘一致）；8 线程 + 持续 `.3` 碰撞注入下 48 个实例号码唯一且首号归属不变、重载一致；**4 个真实子进程**经 `tests/mp_worker.py` 并发分配同一文件映射，106 个来源/假名一一对应 |
| `tests/test_public_api.py` | 13 | 公开符号完整性；`gateway`/`uid_mapper`/`mapper`/`deid` 兼容垫片；`deidentify()`；JSON 字符串字典与树；列表内自描述私有节点删除；数组根；位置参数、`generator=`/`next_id`/可调用对象等注入与调用约定 |

合成数据位于 `tests/fixtures/`（一份字典 + 同一检查的三个导出批次），
端到端演示 `python3 examples/end_to_end_demo.py` 同样实测全部断言通过。

### 明确不在范围内

题面只要求 UID 字段与私有属性处理，因此姓名（PN）、日期等其他 PHI **不做**匿名化；
不包含也不需要任何第三方依赖；未访问网络、真实目标或凭据。多进程锁使用 macOS/Linux
可用的 `fcntl`，在无 `fcntl` 的平台退化为仅进程内锁。
