# 关系数据库原型：序列分配器（Sequence Allocator）

一个纯标准库实现的数据库序列原型：序列可正向或反向走；每个会话先预留一小批
号码再逐次发放；预留状态可保存/恢复，进程退出遗下的号码允许空缺；管理员可以
改步长或显式指定下一值；到达界限时按配置结束或绕回。

仅依赖 Python 3.11 标准库，无第三方依赖、无网络访问，全部数据为本地合成数据。

## 目录结构

```
workspace/
├── sequence_allocator/        # 实现包
│   ├── __init__.py            # 公开 API 导出
│   ├── exceptions.py          # 异常体系
│   ├── model.py               # 配置、持久化状态、预留/绕回核心逻辑、会话
│   └── catalog.py             # 序列目录、管理变更、JSON 持久化
├── tests/                     # unittest 测试（47 个用例）
│   ├── test_config.py         # 配置与参数校验
│   ├── test_basic.py          # 正/反向发放、短批、到界、会话关闭空缺
│   ├── test_interleaving_admin.py  # 两会话交错 + 改步长/指定下一值
│   ├── test_cycle.py          # 绕回等待/释放语义
│   ├── test_persistence.py    # 保存/恢复、进程退出空缺、坏文件拒绝
│   ├── test_concurrency.py    # 多线程交错取号与并发管理变更
│   └── test_catalog.py        # 目录生命周期与跨序列隔离
├── demo.py                    # 综合演示脚本
└── README.md
```

## 运行方法

```bash
# 运行全部测试
python3 -m unittest discover -s tests -v

# 运行综合演示（两会话交错取号 + 管理变更 + 绕回 + 持久化）
python3 demo.py
```

## 公开 API

### 配置：`SequenceConfig`

不可变（frozen dataclass），由目录创建序列时构造：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 必填 | 序列名称，目录内唯一 |
| `start_value` | `1` | 起点，也是绕回后的起始值 |
| `increment_by` | `1` | 步长；正数正向、负数反向，禁止为 0 |
| `min_value` / `max_value` | 64 位有符号整数范围 | 取值上下界 |
| `cache_size` | `20` | 每个会话单次预留号码个数上限，>= 1 |
| `cycle` | `False` | 到界后是否绕回；`False` 即到界结束 |

### 目录：`SequenceCatalog`

- `create_sequence(name, start_value=1, increment_by=1, min_value=…, max_value=…, cache_size=20, cycle=False) -> SequenceConfig`
- `open_session(name) -> SequenceSession`：打开取号会话。
- `drop_sequence(name)`：删除序列，打开的会话被关闭。
- `alter_increment(name, increment_by)`：**改步长**。收回所有会话尚未消费的
  号段，`watermark` 保持在旧版本已预留到的边界，下一个值 = 旧边界 + 新步长。
- `restart_with(name, next_value)`：**显式指定下一值**。同样收回所有未消费
  号段，下一个发放值精确等于 `next_value`。
- `get_config(name) -> SequenceConfig`、`get_state(name) -> SequenceState`、
  `list_sequences() -> list[str]`。
- `save(path)` / `SequenceCatalog.load(path)`：JSON 原子写保存与恢复。

### 会话：`SequenceSession`

- `nextval() -> int`：发放下一个号码；缓存耗尽时自动预留新的一小批。
- `close()`：关闭会话，未消费的预留号码直接丢弃（允许空缺）；支持上下文管理器。
- 只读属性：`closed`、`version`（绑定的序列版本号）、`cached_count`。

### 状态：`SequenceState`

- `watermark`：已预留到的边界（最近一次预留批次的最后一个值），新建时为
  `start_value - increment_by`。**持久化只保存它**，会话缓存不跨进程恢复。
- `version`：管理变更版本号，每次 `alter_increment` / `restart_with` 加 1。
- `reclaimed_batches`：累计被管理变更收回的号段数。

### 异常

`SequenceError` 为基类，子类：`SequenceConfigError`（非法参数）、
`SequenceNotFoundError`、`SequenceExhaustedError`（到界结束或绕回前等待）、
`SequenceSessionClosedError`。

## 关键语义（与题面逐条对应）

1. **正向/反向**：`increment_by` 正负决定方向；反向序列从 `start_value` 递减。
2. **会话先预留小批再逐次发放**：`nextval` 先消费本地缓存，缓存耗尽时按
   `cache_size` 预留新批；边界处可能拿到不足 `cache_size` 的短批。
3. **预留状态保存/恢复，允许空缺**：只持久化 `watermark`。进程退出后会话内
   未消费的号码不补发，新进程从 `watermark + increment_by` 继续（见演示 4）。
   会话主动 `close()` 丢弃未消费号码同理。
4. **两类管理变更都收回未消费号段**：变更清空全部号段登记、版本号 +1；
   打开的会话被主动失效，且会话自身在 `nextval` 时还有版本号惰性兜底。
   - 改步长：`watermark` 不动，沿新步长从旧边界继续（如边界 6、步长 1→5，
     下一个值为 11，7..10 永久空缺）。
   - 指定下一值：`watermark = next_value - increment_by`，下一个值即
     `next_value`。
5. **到界结束或绕回**：
   - `cycle=False`：能取的短批取完后，再取号抛 `SequenceExhaustedError`。
   - `cycle=True`：绕回前若仍有任何会话持有号段，抛
     `SequenceExhaustedError` 作为"等待"信号（本原型为同步模型，不阻塞）；
     等当前号段全部用完或被 `close()` 释放后，下一次预留即从
     `start_value` 绕回。绕回后允许复用值。
6. **两会话交错演示**：`demo.py` 演示 1 中 A、B 各自预留 3 个号交错发放，
   改步长后 A 得 11、B 得 26，`restart_with(100)` 后 A 得 100、B 到界结束。

## 验证范围（实测证据）

测试命令 `python3 -m unittest discover -s tests -v`，共 **47 个用例，全部通过**
（并发用例已重复 5 轮验证无偶发失败）。覆盖：

- **正常场景**：正/反向发放、自定义步长、多会话不相交批次、上下文管理器。
- **边界**：到 `max_value`/`min_value` 的短批、非绕回到界结束（且结束状态
  粘性）、64 位整数域、配置非法值（0 步长、布尔值、越界起点、缓存 0 等）。
- **交错与管理变更**：两会话精确值断言、改步长从旧边界沿新步长继续、
  收回号码不再分发、`restart_with` 精确下一值、连续两次变更版本号递增、
  正反向翻转、反向序列改步长后立即撞界、会话惰性自愈。
- **绕回**：单会话号段用完后绕回、多会话持号时等待（双向均验证）、
  `close()` 释放后立即绕回、反向绕回、非绕回对照。
- **持久化**：多序列往返一致、模拟进程退出后的号码空缺、原子替换无临时文件
  残留、坏格式/损坏状态被拒绝。
- **并发**：8 线程 × 200 次取号全局唯一；4 线程取号与 4 次改步长并发无重复、
  无异常；带同步屏障的并发 `restart_with` 分段唯一性与精确下一值断言。
- **目录生命周期**：不存在序列报错、删序列关闭会话、序列间状态隔离、
  状态快照为独立副本。

演示脚本 `python3 demo.py` 的实际输出与代码内注释逐条核对一致。

## 设计边界与取舍

- 单进程多线程模型，锁粒度为"目录锁 + 每序列 RLock"；不涉及跨进程协调。
- 绕回等待采用"抛异常 + 调用方重试"而非阻塞，避免持锁等待造成死锁；
  调用方用完手中号码或关闭会话后重试即可。
- 持久化格式带 `format_version`（当前为 1），仅恢复全局 `watermark` 与版本，
  不恢复会话级缓存——这正是"允许空缺"语义的来源。
- `restart_with` 与方向翻转可能复用此前发放过的值，这是管理操作的显式语义；
  唯一性保证仅在同一版本谱系（无 `restart`、无绕回）内成立。
