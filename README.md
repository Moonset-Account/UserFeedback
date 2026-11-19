# docredact — 共享文本对象的局部保密清除

一个离线导出器：在无环（DAG）文档对象图上，按「页面 → 实例路径」定位**一处**
文本实例，删除其中的字符区间，并在序列化时只保留仍可从公开页面到达的对象。

题面场景：同一份可编辑文本对象被多个页面复用（例如同一段公告出现在两页）。
要求只清除其中一处，其他页面复用的原文必须保持原样、且仍可编辑。

## 入口

Python 3.11.6（本工作区 `.venv` 即为该版本）。所有命令都在仓库根目录执行。

```bash
# 验收测试（真实断言，92 个用例）
.venv/bin/python -m unittest discover -s tests -t .

# 也支持从仓库根直接发现
.venv/bin/python -m unittest

# 命令行：清除 → 导出 → 重新抽取
.venv/bin/python -m docredact redact \
    --input examples/sample_document.json \
    --output /tmp/redacted.json \
    --page p1 --path main.body \
    --start 38 --end 55 --report
.venv/bin/python -m docredact extract --input /tmp/redacted.json --page p1
.venv/bin/python -m docredact extract --input /tmp/redacted.json --page p1 --what speech
```

`--path` 可传点分字符串（`main.body`），也可重复传参（`--path main --path body`）。
默认只序列化公开页面；`--include-private` 可保留私有页面（孤立对象仍然不会写出）。

库入口：

```python
from docredact import Document, verify_export, extract_page_text

doc = Document.load("input.json")
result = doc.redact("p1", ("main", "body"), start=38, end=55)
text = extract_page_text(result.document, "p1")
data = result.document.export()      # 只含公开可达对象
verify_export(data)                  # 独立复核导出闭包
```

## 数据模型

```jsonc
{
  "format": "docredact/1",
  "objects": {
    "txt":  {"kind": "text", "text": "…", "speech": [
              {"start": 0, "end": 14, "utterance": "…"}]},
    "inst": {"kind": "instance", "ref": "txt"},
    "cont": {"kind": "container", "children": {"key": "object-id"}}
  },
  "pages": {"p1": {"root": "cont", "public": true}}
}
```

- 对象图必须无环：环、自引用、悬空引用都会被拒绝（拓扑排序检测）。
- 页面用 `root` 指向对象；`public` 缺省视为公开。
- 路径是从页面根出发、逐级经 `children` 键下行的键序列；末端必须落在
  `instance` 上（正文对象本身不能直接作为清除目标）。页面根本身就是实例时用空路径。
- 字符位置按 **Unicode 码点**计（Python `str` 下标），既不是 UTF-16 码元也不是字节。
  半开区间 `[start, end)`。

## 语义

**局部清除 `redact(page, path, start, end)`**

1. 沿该页面的引用路径做写时复制：只复制路径上的容器、实例和目标文本对象。
   目标文本对象生成新 id（`txt~redacted`），实例指向它，页面根改指向新的容器链。
2. 其他页面仍指向原始对象，因此不受影响，并且可以继续通过原对象编辑
   （共享对象的编辑会正常传播到所有仍引用它的页面）。
3. 朗读片段（辅助朗读文本）与原文区间对应：
   - 与删除区间**相交**（`seg.start < end and seg.end > start`）的片段**整段移除**；
   - 起点在删除区间之后（`seg.start >= end`）的片段整体左移 `end - start`；
   - 只是**相邻**（首尾相接触但不重叠）的片段保留。
4. 返回的 `RedactionResult` 记录删除的原文、被整段移除的朗读片段、新旧对象 id。
   输入文档本身不被修改。

**编辑原文 `edit_text(object_id, start, end, replacement)`**

对共享文本对象就地编辑，所有引用它的实例可见。朗读片段按同一规则处理：
被触及的片段整段丢弃，之后的片段按长度差平移。非法编辑（倒置区间、越界、
在片段内部插入等导致坐标不可表达的情况）会抛错且对象保持不变。

**导出 `export(public_only=True)`**

- 只序列化从公开页面可达的对象；私有页面及其独占对象不写出。
- 孤立对象（任何页面都不可达）不写出。
- 输出中对象与页面按键排序，结果确定；导出后重新加载仍能通过校验。
- `export(public_only=False)` 保留私有页面，但孤立对象依然不写出。

**复核 `verify_export(data)`**

独立于 `export()` 重新读取序列化结果，检查：不含私有页面、所有已序列化对象
都能从公开页面到达、页面根都存在。返回可重新抽取的文本与朗读片段。

## 验证范围

`tests/` 共 92 个 unittest 用例，全部执行真实断言，覆盖：

| 文件 | 范围 |
| --- | --- |
| `test_local_redaction.py` | 只清除选中实例；其他页、同页兄弟实例不受影响；共享原文仍可编辑；结果元数据 |
| `test_shared_instances.py` | 两个实例共享同一文本；同页两处实例；别名容器；连续清除的坐标推进；根即实例的空路径 |
| `test_speech_remapping.py` | 相交片段整段移除、相邻片段保留、后续片段左移、他页朗读不变 |
| `test_boundaries.py` | 区间恰好止于片段起点/始于片段终点；跨越多个片段；全删；空隙内删除；星光面邻接 |
| `test_code_points.py` | 星光面字符（UTF-16 双码元）、组合附加符号、CJK、emoji+VS16 均按码点计 |
| `test_editing.py` | 编辑共享原文向各页传播；编辑私有副本仅影响本页；插入/替换的片段处理；非法编辑回滚 |
| `test_rejections.py` | 倒置/空/越界/非整数区间、未知页面、路径穿过文本或止于容器/文本、成环、悬空引用、非法朗读区间、坏 JSON |
| `test_export.py` | 私有页与孤立对象不写出；导出闭包自洽；落盘往返；对导出重新清除；确定性 |
| `test_verify_export.py` | 干净导出通过复核；带私有页或不可达对象的导出被拒绝；JSON 往返后复核 |
| `test_invariants.py` | 固定种子随机交错清除/插入/替换 120 步，逐步校验图合法、片段坐标有效、清除不跨页泄漏、导出可复核 |
| `test_cli.py` | 命令行端到端：清除+报告、抽取正文/朗读、多段 `--path`、非法参数非零退出且不落盘 |

可接受结果（其他页保留原文）、应被拒绝结果（越界/倒置/成环/私有页泄漏）
以及影响两者的边界或交错场景（相邻片段、共享实例、别名容器、随机交错）均有覆盖。

已知限制（不属于本任务范围）：不做目标探测或网络访问；不解析真实飞书二进制格式，
输入输出均为本仓库定义的 JSON 对象图；测试仅使用本地合成夹具。
