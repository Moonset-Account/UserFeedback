# 科研资料页增量接口采集（RIP/1.0）

一个从零可运行的演示项目：本地源站以 **GraphQL 增量交付风格的固定协议子集 RIP/1.0**
（NDJSON：初始对象、带路径延迟字段补丁、列表插入补丁、局部错误、结束标志）推送虚构的
科研资料，采集器分开维护**暂存树**与**可发布结果**，处理乱序补丁、子先于父、列表插入
位置漂移、重复补丁、非法路径、局部错误与流中断，并在中文网页上逐步展示每条补丁如何
改变结果树。

> 全部数据为虚构，不依赖真实账户、外部业务服务或 Docker。协议为本项目自定的固定
> 示例约定，**不要求兼容任何 GraphQL 草案**，声明支持的子集见
> [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

## 1. 环境要求

- Python 3.11+（开发验证版本：3.11.6）
- 浏览器自动化：Playwright（仅测试需要，Chromium 内核）

## 2. 安装依赖

```bash
python3 -m pip install -r requirements.txt          # 运行所需
python3 -m pip install -r requirements-dev.txt      # 测试所需（含 pytest、playwright）
python3 -m playwright install chromium              # 下载端到端测试用浏览器（已装可跳过）
```

## 3. 启动方式（统一入口，无需手动激活虚拟环境）

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 即可操作。若当前解释器缺少运行依赖，`app.py` 会自动在
项目内创建 `.venv`、安装 `requirements.txt` 并以该环境重新执行自身；也可加
`--no-bootstrap` 强制使用当前解释器。

页面操作：

1. 左栏选择场景与采集接口（HTTP 增量流 / 本地 NDJSON 文件），点“运行该场景”；
2. 或直接点右上角 **运行示例**（`id="run-demo"`），用小样例依次跑一个正常场景和一个
   边界（流中断）场景，目标 15 秒内结束，两个结果都保留可切换查看；
3. 右栏拖动“快照回放”滑块查看每一步补丁后的结果树；展开“来源证据”可逐条查看
   接受/挂起/重放/拒绝决策，点击任意行查看原始消息；字段错误与中断原因在“状态与错误”
   卡片中展示并保留来源 `seq`。

页面按 1280×720 适配，关键操作与结果在首屏可见，证据细节放在可展开区域。

## 4. 测试命令

```bash
# 全部测试（32 个：引擎单元 + 服务端集成 + Playwright 端到端）
PYTHONPATH=src python3 -m pytest tests/ -q

# 仅核心逻辑与独立真值断言
PYTHONPATH=src python3 -m pytest tests/test_engine.py -q

# 仅服务端集成（真实 HTTP NDJSON 流 + Flask API）
PYTHONPATH=src python3 -m pytest tests/test_server.py -q

# 仅浏览器端到端（自动拉起 python3 app.py）
PYTHONPATH=src python3 -m pytest tests/test_e2e_playwright.py -q
```

## 5. 目录结构与职责分离

```
app.py                         统一启动入口（含 .venv 自动引导）
requirements*.txt              依赖声明
docs/PROTOCOL.md               RIP/1.0 协议子集规范（消息、路径、状态机）
src/
  rip_collector/               采集器（核心）
    parser.py                  NDJSON 消息解析与形状校验
    paths.py                   RFC 6901 路径（jsonpointer 库）与合法性校验
    engine.py                  暂存树/待解析队列/插入缓冲 + 不可变发布快照
    transport.py               公开接口传输：HTTP 流 或 NDJSON 文件
  sample_generator/            样例生成器（与采集器分离，只产出协议消息）
    scenarios.py               7 个虚构场景（2 演示 + 5 验收）
  web/                         Flask 源站 + 采集 API + 中文页面
    source.py                  公开源站接口 /source/stream/<场景>
    api.py                     采集控制接口 /api/...
    task_manager.py            任务线程与结果留存
    templates/ static/         中文操作页
tests/
  expected_truth.py            独立手写真值（仅测试使用，生产代码禁止 import）
  conftest.py                  测试夹具（采集只走公开入口）
  test_engine.py               核心逻辑 + 四种验收情况 + 重跑去重
  test_server.py               服务端/HTTP 集成
  test_e2e_playwright.py       浏览器端到端
runtime/recordings/            运行时生成的 NDJSON 录制文件（已 gitignore）
```

**分离约束**：采集器与样例生成器互不依赖（`rip_collector` 不 import
`sample_generator`）；采集只能通过公开 HTTP 接口、NDJSON 文件或浏览器交互进行。
`tests/expected_truth.py` 是测试专属独立真值，生产代码不读取它来代替采集。

## 6. 协议子集摘要（详见 docs/PROTOCOL.md）

| 消息 | 语义 | 关键处理 |
| --- | --- | --- |
| `initial` | 初始对象，建立暂存树根 | 重复 initial 拒绝 |
| `patch` | JSON Pointer 路径写值 | 父节点缺失则挂起，父节点到达后按 seq 重放；同 seq 去重 |
| `insert` | 数组逻辑槽位插入 | 按 `(index, seq)` 排序、`item_id` 去重后重建列表，容忍乱序/漂移/重复 |
| `error` | 字段级局部错误 | 结果中保留错误占位与来源 seq；后续 patch 可恢复该字段 |
| `end` | 结束标志 | 有未决项时为 `ended_unresolved`；未收到 end（连接/EOF）为 `interrupted`，均不标完整 |

任务状态机：`streaming` →（收到 end 且无挂起）`complete`；→（收到 end 但有挂起）
`ended_unresolved`；→（传输中断/EOF 无 end）`interrupted`。后三者之外任何情况下
`complete` 恒为 `false`。

## 7. 验收情况对照

| 验收要求 | 覆盖测试 |
| --- | --- |
| 补丁乱序（含子先于父） | `test_child_patch_replayed_after_parent_arrives`、`test_out_of_order_list_converges` |
| 重复补丁 / 重复列表项 | `test_duplicate_seq_and_item_dedup` |
| 部分字段出错且保留错误来源 | `test_field_errors_keep_source_and_recover` |
| 未收到结束标志，暂存树不标完整 | `test_demo_edge_interrupted_never_complete`、`test_interrupted_task_never_marked_complete` |
| 非法路径拒绝且留证、流不中断 | `test_illegal_paths_are_rejected_with_evidence` |
| 重跑不产生重复列表项 | `test_rerun_produces_no_duplicate_list_items` |
| 真实 HTTP 采集与页面操作 | `test_server.py`、`test_e2e_playwright.py` |

## 8. 已知边界

- 仅支持 NDJSON over HTTP 与 NDJSON 文件两种传输，不支持 multipart 等其他封装；
- 列表采用“插入事件缓冲 + 发布时重建”模型，适合本演示规模；超大列表的增量物化
  未做专门优化；
- 任务结果保存在内存中（最多保留最近 50 个任务），进程重启后清空。
