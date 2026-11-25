# 取证芯片转储读取器

一个仅依赖 Python 标准库的取证一致性读取器。输入若干等长芯片转储、有限候选通道排列、每芯片允许的字节反转方式以及有来源的已知字节/页摘要后，它会：

1. 按输入指定的字长，以“每个通道轮流取一个字块”的方式重建每个候选逻辑镜像；
2. 用已知字节和带页长的页校验摘要淘汰不符候选；
3. 在读取任意逻辑区间时逐字节计算剩余候选的共识；
4. 把所有剩余候选一致的字节标为已确认，把其余位置组织成歧义段，并给出至少两种字节解释及支持该解释的接线；
5. 保留证据版本、淘汰原因和历史回答，支持加入新证据后重新回答与追溯。

运行环境按题目要求使用 Python 3.11.6；没有安装或联网依赖。

## 运行测试

在当前工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

也可以做语法编译检查：

```bash
python3 -m compileall -q forensic_reader tests
```

最近一次本地实测结果为：

```text
Ran 27 tests in 0.004s

OK
```

## 核心重建规则

设：

- 有 `N` 个等长芯片，每个转储长度为 `L`；
- 字长为 `W` 字节；
- 某候选接线的通道顺序为 `[c0, c1, ..., cN-1]`。

逻辑镜像长度固定为 `N * L`。对每个芯片偏移 `base = 0, W, 2W, ...`，依次追加：

```text
chip[c0][base:base+W], chip[c1][base:base+W], ...
```

若 `L` 不是 `W` 的整数倍，最后一个较短字块也会按同样规则拼接。

支持的反转/变换名称（大小写、连字符和下划线会归一化）：

| 模式 | 含义 |
| --- | --- |
| `"none"`、`"normal"`、`False` | 不变换 |
| `"word"`、`"word_swap"`、`"byte_swap"`、`"endian"` | 在每个 `W` 字节字块内反转字节顺序 |
| `"chip"`、`"reverse"`、`True`、`"full"` | 反转整片芯片转储 |
| `"bit_reverse"` | 可选：反转每个字节内的位顺序 |
| `"complement"`、`"invert"` | 可选：逐字节取反 |

也可以传模式序列表示组合变换。显式消歧时，字内交换请用 `"word"`，整片反转请用 `"chip"`。

## 公开 API

包名为 `forensic_reader`，主要导出：

```python
from forensic_reader import (
    ForensicReader,
    Wiring,
    KnownByte,
    PageChecksum,
    ReadReport,
    Segment,
    Interpretation,
    TraceReport,
    compare_reports,
    generate_wirings,
)
```

`Reader`、`ForensicImageReader`、`ChipForensicReader` 和 `create_reader` 都是 `ForensicReader` 的别名。

### 1. 创建读取器

```python
from forensic_reader import ForensicReader

chips = {
    "U1": bytes([0x00, 0x01, 0x02, 0x03]),
    "U2": bytes([0x10, 0x11, 0x12, 0x13]),
}

wirings = [
    {"id": "normal-order", "order": ["U1", "U2"]},
    {
        "id": "swapped-u1-words",
        "order": ["U1", "U2"],
        "reversal": {"U1": "word", "U2": "none"},
    },
    {"id": "reverse-order", "order": ["U2", "U1"]},
]

reader = ForensicReader(chips, wirings, word_size=2)
```

`chips` 也可以传字节序列，此时芯片 ID 为整数 `0, 1, ...`；也支持 `[(chip_id, data), ...]` 形式。

如果不显式给 `wirings`，可用 `permutations` 与 `reversal_modes` 生成笛卡尔积：

```python
reader = ForensicReader(
    chips,
    word_size=2,
    permutations=[("U1", "U2"), ("U2", "U1")],
    reversal_modes={"U1": ["none", "word"], "U2": "none"},
)
```

省略 `permutations` 时会生成全部芯片排列；省略 `reversal_modes` 时所有芯片均为 `"none"`。

### 2. 加入有来源的证据

已知字节：

```python
reader.add_known_byte(
    address=0,
    value=0x00,
    source="reset vector / lab note 7",
)
```

页摘要。页编号按 `start = page * page_size` 解析：

```python
import hashlib

digest = hashlib.sha256(expected_page_bytes).hexdigest()
reader.add_page_checksum(
    page=0,
    page_size=16,
    algorithm="sha256",
    digest=digest,
    source="programmer verify log",
)
```

也可以描述任意显式区间：

```python
reader.add_page_checksum(
    start=32,
    end=48,
    algorithm="crc32",
    digest="...",
    source="controller page record",
)
```

支持 `hashlib` 提供的摘要名，以及内置 `"crc32"`、`"adler32"`、`"xor8"`、`"sum8"`、`"sum16"`、`"sum32"`。注意 XOR/加法和对字节顺序不敏感，可能无法区分仅排列不同的候选；这正是保守共识机制会继续保留歧义的原因。摘要可传十六进制字符串、原始摘要字节，也可写成 `"sha256:<hex>"`。

还可以统一传证据对象或字典：

```python
from forensic_reader import KnownByte, PageChecksum

reader.add_evidence(KnownByte(address=3, value=0x11, source="datasheet"))
reader.add_evidence({
    "type": "page_checksum",
    "page": 1,
    "page_size": 16,
    "algorithm": "sha256",
    "digest": "...",
    "source": "page label",
})
```

构造函数也接受 `known_bytes=[...]`、`pages=[...]`，或用 `page_digests=[...]` 加统一 `page_size` 批量给出页摘要。

### 3. 读取逻辑区间

区间采用 Python 的半开区间 `[start, end)`：

```python
report = reader.read(0, 16)          # 地址 0..15
report = reader.read(16, length=16)  # 地址 16..31
report = reader.read(address=16, size=16)
report = reader[0:16]
```

只有整个区间全部确认时，才能直接取出唯一字节串：

```python
data = reader.read_bytes(0, 16)
# 等价于 reader.read(0, 16).confirmed_bytes()
```

若区间仍有歧义，`confirmed_bytes()` 会抛出 `AmbiguousReadError`，但报告本身仍可用于展示所有解释。

`ReadReport` 的关键属性：

- `state`：`"confirmed"`、`"ambiguous"`、`"no_consistent_wiring"` 或 `"empty"`；
- `fully_confirmed`：区间内是否每个字节都已确认；
- `consistent` / `has_consistent_wiring`：是否还有候选接线；
- `survivor_ids`：当前通过全部证据的接线；
- `segments`：已确认段或歧义段；
- `byte_states`：每个地址的状态、可能值和各接线给出的值；
- `to_dict()`：转换为 JSON 友好的字典（字节显示为 `hex`）。

已确认段包含共同的 `data`/`hex` 和全部支持接线 ID。多个接线因重复内容而产生相同字节时，该段仍为 `confirmed`，但 `wiring_ids` 会列出多个接线。

歧义段包含：

```python
segment.status == "ambiguous"
segment.interpretations  # 至少两个 Interpretation
interpretation.data      # 该解释对应的整段字节
interpretation.hex
interpretation.wiring_ids
```

分段会在“哪些接线在当前连续位置上给出相同字节”的分组关系改变时断开，因此每段内同一组接线始终支持同一个字节解释。

### 4. 增加证据、重新回答和追溯

每次成功加入证据都会产生一个新版本（初始状态为版本 0）：

```python
entry = reader.add_known_byte(8, 0xEA, source="analyzer")

reader.version                  # 当前证据版本
reader.history                  # 所有 HistoryEntry
entry.before_ids
entry.after_ids
entry.removed_ids
entry.rejections                # 每个被淘汰接线的 expected/actual/reason/source
```

重新回答只需再次调用 `read(...)`。若要直接追溯同一区间在所有证据版本下的判断：

```python
trace = reader.trace(0, 64)

trace.initial.state             # 加证据前的判断
trace.current                   # 最新判断
for judgment in trace.entries:
    judgment.version
    judgment.evidence_summary
    judgment.report
    judgment.diff_from_previous
```

`diff_from_previous.newly_confirmed` 给出因新证据而新确认的连续范围和字节；`removed_wiring_ids` 给出刚被淘汰的接线；若最后一个候选也被矛盾证据淘汰，则 `lost_ranges` 标出变为 `no_consistent_wiring` 的范围。

还可以：

```python
branched = reader.with_evidence(KnownByte(...))  # 深拷贝后再加证据，原读取器不变
reader.report_at_version(0, 0, 64)              # 不改变当前状态，重建历史判断
reader.answer_history_for(0, 64)                  # 之前对该区间的实际 read() 回答
```

不可变报告对象本身也可长期保存，作为“原先判断”的快照。

## 快速示例

```python
import hashlib
from forensic_reader import ForensicReader

chips = {
    "A": bytes([0x00, 0x01, 0x02, 0x03]),
    "B": bytes([0x10, 0x11, 0x12, 0x13]),
}
reader = ForensicReader(
    chips,
    [{"id": "AB", "order": ["A", "B"]},
     {"id": "BA", "order": ["B", "A"]}],
    word_size=2,
)

print(reader.read(0, 8).state)  # ambiguous
reader.add_known_byte(0, 0x00, source="reset vector")
print(reader.read_bytes(0, 8).hex())
# 0001101102031213

trace = reader.trace(0, 8)
print(trace.initial.state, "->", trace.current.state)
# ambiguous -> confirmed
```

## 重复内容不会连坐整页

共识单位是**逻辑字节值**，不是接线身份。若两个不同排列在某页上因重复填充、镜像或相同芯片内容而生成完全相同的字节，则：

- 两个接线都可能通过该页摘要；
- 该页对应字节仍标记为 `confirmed`；
- 报告的 `wiring_ids` 会显示多个接线共同支持这些字节。

`tests/test_consensus_evidence.py::test_repeated_bytes_keep_page_confirmed_under_multiple_wirings` 用合成的 `0x5A` 重复前缀和 SHA-256 页摘要明确验证了这一点。

## 已验证范围

全部数据均为测试内本地合成数据，未访问网络、真实目标、凭据或外部材料。

- `tests/test_reconstruction.py`
  - 按字块轮流拼接；
  - 字内反转与整片反转；
  - 布尔/别名字法；
  - 显式排列与反转模式的笛卡尔积；
  - 默认全排列；
  - 等长校验、排列合法性和反转模式校验；
  - 序列式芯片输入的整数 ID。
- `tests/test_consensus_evidence.py`
  - 歧义段至少两种解释及支持接线；
  - 已知字节筛选和淘汰原因；
  - SHA-256、CRC-32、XOR/加法校验和多页/多页长摘要；
  - 重复内容下多接线仍产生已确认页；
  - 加入证据后重新回答；
  - 初始判断、查询日志和跨版本 trace；
  - 多候选下的筛选；
  - 所有候选被矛盾证据淘汰时的显式 `no_consistent_wiring`；
  - 非法证据、越界区间和空区间边界。
- `tests/test_boundaries_and_trace.py`
  - 芯片长度不能被字长整除时的尾部短块；
  - 非字对齐任意区间、`length/size/address` 参数和切片；
  - 歧义支持分组变化时的逐段切分；
  - 构造函数批量传入已知字节和页摘要；
  - 多条证据下逐版本追溯及 `report_at_version`；
  - 裸排列和排列列表输入。

## 安全边界

这是题面规定的防御性取证一致性判断：只做本地候选重建、摘要比对、共识分类和审计追溯。程序不包含漏洞利用、入侵、攻击、规避载荷生成或任何联网行为。
