# lock_review — 处置动作流锁序审查库

面向终端防护代理的维护期审查工具：读取维护者导出的**抽象动作流**与**支持的
配置组合**，分析停止可疑对象时隔离（quarantine）与恢复（recovery）等处置
阶段的锁申请顺序，找出可能阻断处置的锁等待环。

## 实际入口

* 命令行：`python -m lock_review <spec.json>`
  —— 向标准输出打印 JSON 报告；发现可能阻断处置的环时退出码为 `1`，否则 `0`。
* 库调用：`lock_review.review(spec)` / `lock_review.review_file(path)`。

本仓库使用 Python 3.11.6（`.venv/bin/python`）。示例：

```sh
.venv/bin/python -m lock_review examples/agent-flows.json
```

## 输入规范（JSON）

```json
{
  "configs": [{"id": "cfg-realtime", "enables": ["realtime", "on-access"]}],
  "exclusive_groups": [["scan-mode", "restore-mode"]],
  "streams": [{
    "id": "stop-object",
    "phase": "quarantine",
    "mode": "stop-mode",
    "conditions": ["realtime"],
    "steps": [
      {"op": "acquire", "lock": "obj-table", "mode": "write"},
      {"op": "cancel", "releases": ["obj-table"]},
      {"op": "release", "lock": "obj-table"}
    ]
  }]
}
```

* `configs`：支持的配置组合，`enables` 为该组合启用的条件集合。
* `exclusive_groups`：互斥运维模式组，组内不同模式不会并发运行。
* `streams[].steps`：`acquire`（含 `read`/`write` 访问模式）、`release`、
  `cancel`（取消路径，释放指定锁后该路径终止）。

## 判定规则（验证范围）

1. 动作流展开为"持有-等待"边：持有锁 A 时申请锁 B 记为边 A→B。
2. 仅当等待环上的所有动作流能在**同一配置组合**里并发出现（启用条件同时
   被满足）且相互等待时，才判为可能阻断处置。
3. **互斥运维模式**里的逆序获取不报（不同模式不并发）。
4. 环上每把锁的持有/等待两侧至少一方为写；**同一入口读锁共存（读-读）
   不算冲突**。
5. 有**取消路径**释放锁时，该路径终止，但其余可能路径继续参与检查，
   不因取消路径而漏报。
6. 报告输出涉及的**处置阶段**（`phases`）与**持有关系**（`holds`：哪个
   动作流持有什么锁、以何种访问模式等待哪把锁），以及可行配置组合。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -t . -v
```

`tests/test_lock_review.py` 使用 unittest 编写，含真实断言，覆盖：

* 可接受结果：互斥模式逆序获取不报、读锁共存不报、无环不报；
* 应被拒绝结果：同配置并发的写锁二环/三环必须报出，且含阶段与持有关系；
* 边界/交错：取消路径释放锁后其他路径仍报、取消后无新申请的路径不报、
  无共同配置组合不报、环上某锁读-读时环不成立、每锁单侧写即足够；
* 实际入口：`python -m lock_review` 的退出码反映是否有发现。

## 目录结构

```
lock_review/          # 审查库（model / analyzer / CLI 入口）
tests/                # unittest 验收用例
examples/             # 本地合成示例规范
```
