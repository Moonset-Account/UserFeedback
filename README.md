# gitverify — 接收端隔离区对象核验

代码托管接收端在更新任何引用之前，对隔离区（quarantine）中的推送对象做只读核验。
纯 Python 标准库实现（`zlib` / `hashlib`），仅支持 SHA-1 对象仓库；不创建工作树，
不执行对象内容，不联网。

## 文件

- `gitverify.py` — 可调用检查模块（实现）
- `tools/local_verify.py` — 本地核验入口（C01–C04，真实断言）
- `README.md` — 本文件

## API

```python
import gitverify

report = gitverify.check_quarantine(
    heads,          # Mapping[ref名, 40位小写hex对象ID]，候选分支头
    trusted,        # Iterable[40位hex]，已登记的可信旧对象边界
    read_object,    # Callable[[str], Optional[bytes]]，按对象ID返回 loose-object
                    # 压缩字节（zlib）；返回 None 或抛 KeyError 表示读不到
    max_object_bytes=None,  # 单对象展开（inflate 后含头部）字节上限
    max_total_bytes=None,   # 累计展开字节上限
    max_objects=None,       # 新可达对象数上限
)
```

返回 `Report`：

- `branches: dict[str, BranchVerdict]` — 逐分支 `ok` 与最短错误引用链
  `chain`（`ok=True` 时为 `None`）。链节点形如 `commit:<oid>` / `tree:<oid>` /
  `blob:<oid>` / `missing:<oid>` / `trusted:<oid>` / `object:<oid>`
  （预算触断时对象头未解析，类型未知，标为 `object:`），末元素为
  `error: <原因>`。链由 BFS 产生，是按引用数计的最短链。
- `objects_verified / objects_trusted / objects_rejected` — 对象计数。
- `bytes_expanded` — 累计展开字节（共享对象只计一次）。
- `halted` — 是否因累计字节或对象数预算触断而停止展开。
- `report.ok` — 全部分支可接收。

也可使用 `QuarantineChecker(read_object, **预算).check(heads, trusted)` 复用同一
检查器。

## 核验语义

- 解压 loose-object，校验头部 `<type> <size>\0`：类型限
  `blob/tree/commit/tag`，声明长度必须等于负载长度，inflate 后全文的 SHA-1
  必须等于对象 ID。
- commit：必须有 `tree` 行（目标必须是 tree），`parent` 行目标必须是 commit。
- tree：原始文件名按字节判重、不得为空、不得含 `/`；`100644/100755/120000`
  项必须指向 blob，`040000` 项必须指向 tree；`160000`（gitlink）保留外部仓库
  commit 标识，**不展开、不读取**；其他 mode 拒绝。
- tag：跟随 `object` 引用并核对声明的 `type`。
- 新可达对象被多分支共享时只读取、只校验、只计预算一次。
- 命中可信边界的对象不读取、不展开、不计预算，直接视为可信。
- 未登记为可信且读不到的对象阻断所有能到达它的分支。
- 预算：单对象展开大小超限是该对象的错误（不全局停止）；累计字节与对象数
  超限会触断对应对象并全局停止展开（`halted=True`），触断对象本身不再被读取
  （对象数预算）或不再被解析（字节预算）。预算计数基于全局已见对象集，分支
  共享不会重复计数。

## 本地核验

```sh
.venv/bin/python tools/local_verify.py --list        # 列出用例
.venv/bin/python tools/local_verify.py --case C01    # 运行单个用例
.venv/bin/python tools/local_verify.py               # 运行全部
```

每个用例输出一行 JSON：`case`、`assertions`（实际断言数）、`passed/failed/
skipped`、`seed`（样本种子标识，样本为确定性合成数据）以及每条断言的
expect/observed 摘要（仅含合成对象 ID 与错误摘要，无敏感内容）。任一断言
失败、断言数为零或整组跳过时退出码非零。

用例覆盖：

- **C01**：本地构造 loose-object 字节；合法链被接受；篡改负载 → SHA-1 不符；
  头部长度虚报；未知类型；commit 的 tree 指向 blob、parent 指向 tree、tree 的
  文件项指向 tree，均按目标类型拒绝。
- **C02**：tree 重名、名字含 `/` 拒绝；文件/符号链接/目录项的目标类型校验；
  gitlink 对象不在库中仍接受且适配器从未被请求该 ID。
- **C03**：两分支共享 commit/tree/blob 只校验一次、字节只计一次、适配器只读
  一次；可信边界对象不被读取且停止展开；未登记缺失对象阻断分支并给出
  `commit -> missing -> error` 链。
- **C04**：分别触发单对象大小（不波及其他分支）、累计字节、对象数预算；
  共享坏对象时不同分支得到各自的最短错误链（4 节点 vs 5 节点）；对象数预算
  触断的对象从未被读取；模块源码不含 `eval/exec/subprocess` 等执行原语；
  gitlink 从不展开。

## 可重放样本

样本全部由 `tools/local_verify.py` 用确定性字节现场构造（固定 seed 标识，
无随机源），例如：

```python
blob  = zlib.compress(b"blob 6\0hello\n")          # 合法 blob
tree  = b"100644 a.txt\0" + bytes.fromhex(oid)     # tree 条目
commit = b"tree <hex>\nauthor ...\n\nmsg\n"        # commit 负载
```

重放：`.venv/bin/python tools/local_verify.py`，退出码 0 且每行
`failed=0` 即通过。

## 未覆盖边界（审计时保留未知）

- 仅 SHA-1 仓库；SHA-256 对象格式未实现。
- 读取适配器只接受 loose-object zlib 字节；pack/delta 解包属于适配器职责，
  本模块不核验 pack 完整性。
- tree 文件名仅拒绝空名、`/` 与字节级重名；`.`、`..`、反斜杠、控制字符、
  大小写折叠冲突、Unicode 规范化冲突未拒绝（接收端如需 checkout 安全应另加
  策略）。
- tree mode 按八进制值接受 `100644/100755/120000/40000/160000`；非规范拼写
  （如前导零）未单独拒绝。
- commit/tag 的 author/committer/encoding/gpgsig 等头字段不做语义校验；
  签名真实性不在本模块范围。
- 可信边界对象的目标类型不再核对（信任登记方）；边界登记错误不在本模块
  可证实范围。
- 预算为单次 `check` 调用内的全局预算；跨推送、跨仓库的配额未覆盖。
- 核验包装器（`tools/local_verify.py`）是运行入口而非合格证明；审计应阅读
  `gitverify.py` 源码与真实运行日志。
