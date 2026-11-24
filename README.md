# forensic_segments — 多成像选段库

同一块只读取证盘的多次成像结果之间常常互补：后一次成像可能读到了前一次失读的
扇区，也可能有一小段与前一次互相矛盾。本库把这些成像登记在一起，按字节区间抽取
证据，并逐段记录**实际采用的映像、原始采集编号、设备身份、段内偏移与文件偏移**，
供另一名调查员复核。

- 纯标准库实现，Python 3.11.6，无第三方依赖。
- 只处理调用方提供的元数据与字节摘要，不打开映像文件、不访问网络。
- 所有模型为不可变 dataclass；报告可序列化为 JSON 或渲染成复核单。

## 目录

```
forensic_segments/
  __init__.py    公开 API
  models.py      Segment / Image / Candidate / FillNote / SegmentSelection / SelectionReport
  library.py     ImageLibrary：登记、来源去重、抽取
  errors.py      ValidationError / UnknownSourceError / SelectionError
  __main__.py    命令行入口
tests/
  _fixtures.py        合成数据构造器
  test_models.py      字段校验与不可变性
  test_registration.py 登记规则、派生复制一致性
  test_eligibility.py 失读填充与来源去重
  test_selection.py   抽取报告、分区不变量、偏好顺序、序列化
  test_cli.py         命令行
```

## 运行

```bash
python3 -m unittest discover -s tests -v
```

需要导入库时把工作区根目录加入 `sys.path`（测试已自动处理），例如：

```bash
python3 -c "import sys; sys.path.insert(0,'.'); import forensic_segments"
```

命令行（元数据为 JSON，全部为本地合成数据）：

```bash
python3 -m forensic_segments meta.json --start 0 --end 4096
python3 -m forensic_segments meta.json --start 0 --end 4096 --prefer pass2.img --json
```

元数据格式：

```json
{"images": [
  {"image_id": "pass1.img", "device_identity": "NVMe-SN-1",
   "capture_id": "CAP-1", "segment_size": 512,
   "segments": [
     {"index": 0, "status": "read", "digest": "sha256:aa"},
     {"index": 1, "status": "unreadable", "fill_byte": 0},
     {"index": 2, "status": "not_imaged"}
   ]},
  {"image_id": "copy1.img", "derived_from": "pass1.img", "...": "..."}
]}
```

`derived_from` 可选；声明为复制的映像必须在登记时与父映像逐段一致，否则拒绝。

## 公开 API

### 值对象

| 名称 | 作用 |
| --- | --- |
| `Segment(index, status, digest=None, fill_byte=None)` | 一个定长扇区在一次成像中的状态。`status` 取 `SegmentStatus.READ` / `UNREADABLE` / `NOT_IMAGED`。`READ` 必须带非空 `digest`；`UNREADABLE` 必须带 `0..255` 的 `fill_byte`；`NOT_IMAGED` 两者都不带。`is_evidence` 仅对 `READ` 为真。 |
| `Image(image_id, device_identity, capture_id, segment_size, segments, derived_from=None)` | 一次成像产出的文件。`capture_id` 是**原始采集编号**，复制同一原始采集的多个文件共用它。段索引必须从 0 连续。 |
| `SegmentStatus` | 三态枚举：`read` / `unreadable` / `not_imaged`。传入等值字符串会被规范化。 |
| `Candidate` | 报告内某个映像在某段上的状态快照（含 `eligible`、`rank`）。 |
| `FillNote` | 失读填充占位记录：位置、段号、映像、采集编号、填充字节；`covered=True` 表示同区间已被别次成像的确认读覆盖。 |
| `SegmentSelection` | 一个被采用的子区间：`start/end`、`segment_index`、`offset_in_segment`、`file_offset`、`image_id`、`capture_id`、`device_identity`、`digest`、`conflicted`、`candidates`；派生属性 `corroborating_captures` / `dissenting_captures`。 |
| `SelectionReport` | 抽取结果，见下。 |

### `ImageLibrary`

| 方法 | 说明 |
| --- | --- |
| `add_image(image)` / `add_images(seq)` | 登记。重复登记完全相同的映像为空操作；同一 `image_id` 内容不同、段大小不一致、`derived_from` 未登记或复制内容/采集编号/设备身份与父映像不符时抛错。 |
| `images` / `image_ids` / `segment_size` | 只读视图。 |
| `capture_ids()` | 去重后的原始采集编号，按登记顺序。重复复制只出现一次。 |
| `device_identities()` / `images_for_device(id)` | 设备身份集合与查询。 |
| `image(id)` / `images_for_capture(id)` | 查找，未知 id 抛 `UnknownSourceError`。 |
| `segment_candidates(i)` | 每个映像在第 `i` 段的候选状态。 |
| `confirmed_captures(i)` | 第 `i` 段上**有确认读**的原始采集（去重）。 |
| `fill_images(i)` | 第 `i` 段上带失读填充的映像。 |
| `extract(start, end, *, preferred=None)` | 抽取 `[start, end)`，返回 `SelectionReport`。 |
| `select_segment(i, *, preferred=None)` | 抽取整段；该段无任何确认读时抛 `SelectionError`。 |

`preferred` 为映像 id 序列（不接受裸字符串），按顺序优先；同一原始采集的第二个
文件会被忽略并记入 `warnings`，因为它不构成第二个来源。未被列出的原始采集仍可
在优先映像没有确认读时补位；未被列出的映像按登记顺序排在其后。

### `SelectionReport`

| 字段 / 属性 | 含义 |
| --- | --- |
| `requested_start` / `requested_end` / `length` | 请求区间。 |
| `segments: tuple[SegmentSelection, ...]` | 每个被采用子区间的映像与偏移。**可用与冲突区间都会出现在这里**，由 `conflicted` 区分。 |
| `available_ranges` | 有确认读且各来源摘要一致的子区间。 |
| `conflict_ranges` | 有确认读但摘要互相矛盾的子区间。 |
| `missing_ranges` | 无任何确认读的子区间（失读填充或未成像）。 |
| `fill_notes` | 每个涉及请求区间的失读填充记录；`covered=True` 表示随后被别次成像读到。 |
| `available_length` / `conflict_length` / `missing_length` / `covered_length` / `selected_length` | 各分类字节数。`covered_length = available + conflict`。 |
| `coverage` | `covered_length / length`；空区间视为 1.0。 |
| `is_complete` | 无缺失且无冲突。 |
| `covering(offset)` | 返回覆盖该字节的 `SegmentSelection` 或 `None`。 |
| `segments_for_image(id)` | 按映像筛选。 |
| `warnings` | 去重来源、采集自相矛盾、缺失/冲突等提示。 |
| `to_dict()` / `to_json()` / `render()` | 结构化与人类可读两种复核形式。 |

`available_ranges`、`conflict_ranges`、`missing_ranges` **恰好划分**请求区间：
两两不重叠、并集等于 `[start, end)`。该不变量在测试中用多组区间切片逐点校验。

## 判定规则

1. **只有确认读可被引用。** `UNREADABLE` 的填充字节保留位置（使后续段偏移不变），
   但永远不作为证据；`NOT_IMAGED` 表示该次成像未到达该段。
2. **重复复制只算一个来源。** 共用 `capture_id` 的文件（无论是否声明
   `derived_from`）在 `confirmed_captures`、`corroborating_captures` 与冲突判定中
   都只计一次。同一采集的两个非派生文件若摘要互相矛盾，会进入 `warnings`，并且
   该区间被判为冲突——因为此时连"原始采集"这一来源自身也不自洽。
3. **冲突不隐藏。** 冲突区间同样给出采用的映像与偏移（按优先级选第一个确认读），
   同时在 `conflict_ranges`、`warnings` 和 `render()` 的 `differs from` 部分列出
   所有异见映像，供调查员决定采信哪一个。
4. **偏移可复核。** `file_offset` 是所采用映像文件内的字节偏移
   （`segment_index * segment_size + offset_in_segment`），并给出 `file_offset_end`。

## 验证范围

测试全部基于本地合成元数据，覆盖：

- 正常场景：多次成像互补，后一次读到此前的失读段；整段与段内任意切片。
- 失读填充：位置保留、后续段偏移正确、填充永不入选、`fill_notes` 裁剪到请求区间、
  被后续成像恢复时 `covered=True` 而仍记录填充来源。
- 冲突：跨采集摘要矛盾、同一采集自相矛盾、冲突区间裁剪、冲突时仍记录采用来源。
- 来源去重：派生复制与同采集重复文件只计一个来源；复制内容/采集/设备不符被拒绝。
- 边界：空区间、越界区间、反向区间、负数、非整数、布尔值、全镜像无确认读、
  跨段区间偏移、短映像（未成像段）。
- 分区不变量：对规范场景按 97/131 步长枚举 100+ 组 `(start, end)`，逐点验证
  可用/冲突/缺失三类精确划分。
- 序列化：`to_dict`/JSON 往返、`render()` 文本内容。
- 命令行：文本与 JSON 输出、`--prefer`、非法区间、缺失文件、格式错误的元数据。

实测结果：`python3 -m unittest discover -s tests -v` → **85 个测试全部通过**
（Python 3.11.6，macOS）。上述结论来自本地实际执行，未接触真实介质或网络。
