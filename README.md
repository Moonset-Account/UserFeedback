# 命令面板 RPA 实验台

在**本地模拟实验控制界面**上，通过命令面板与多层菜单执行指定配置操作的 RPA 项目。
全部数据为虚构，不连接、不控制任何真实设备，也不是通用命令行工具。

## 它解决什么问题

模拟实验台的菜单里刻意构造了真实自动化中最难处理的几类情况：

| 难点 | 本项目的构造 | RPA 的处理 |
| --- | --- | --- |
| 同名命令不同作用域 | 三个同名“启动”：`实验 > 设备 > 温控台/真空泵/光源 > 启动` | 只按**完整菜单路径 + scope** 识别；后缀命中多个且无法消歧时判定歧义并**停止** |
| 禁用项 | BETA 上下文下真空泵“启动”灰显（联锁未解除），禁用规则随上下文变化 | 点击前检查 `enabled`，禁用即**停止且不点击**，错误码 `COMMAND_DISABLED` |
| 动态子菜单 | `实验 > 实验预设` 悬停后异步加载，内容随 ALPHA/BETA 上下文不同 | 识别 `dynamic` 节点，等待加载占位消失后再定位子命令 |
| 搜索排序变化 | 同一查询“启动”在 ALPHA/BETA 下首条结果不同（上下文亲和度 + 近期使用加权） | 只读取 DOM 实时顺序并保留顺序证据，**不做本地重排、不按序号点击** |
| 快捷键冲突 | `Ctrl+L` 同时绑定光源启动与视图刷新，页面横幅显式提示 | RPA 只走完整菜单路径，不依赖快捷键；冲突写入快照证据 |

执行器在每个动作后验证**明确可见的状态变化**（设备状态卡、当前预设、当前上下文的 DOM 文本），
失败立即停止（stop-on-error），并保留逐步决策理由、观测值与截图证据。

## 目录结构

```
app.py                     统一入口（自动创建 .venv、装依赖、装 chromium 后重启自身）
requirements.txt
PROTOCOL.md                任务指令协议 v1 的子集声明
src/
  rpa/                     核心 RPA 逻辑（不导入 sample_data/tests，不读真值目录）
    protocol.py            JSON 协议子集解析（标准库 json，重复键/体积/未知键校验）
    menu_model.py          菜单快照数据模型
    resolver.py            纯逻辑命令决策：路径匹配、歧义、禁用、动态菜单
    collector.py           Playwright 采集/执行器 + 访问边界路由守卫
    executor.py            任务编排、证据报告、失败即停
    errors.py              稳定错误码体系
  sample_data/generator.py 样例生成器（与采集器分离，同时产出任务与独立真值）
  webapp/
    lab_registry.py        被测实验台的菜单注册表（虚构数据）
    server.py              Flask：/lab 被测系统 + / 控制台 + /api 任务管理
    static/lab/            被测模拟实验台页面（HTML/CSS/JS）
    static/console/        中文控制台页面（含 id=run-demo 按钮）
tests/                     协议/解析器单测、隔离守卫、Web API 冒烟、Playwright 端到端断言
data/
  samples/                 生成的任务样例（JSON）
  ground_truth/            独立真值（*.truth.json），仅供 tests 使用
  runs/                    每次运行的 report.json 与 evidence/*.png（git 忽略）
```

## 依赖安装与启动

要求 Python 3.11+。无需手动创建或激活虚拟环境——入口会自动完成：

```bash
python3 app.py --host 127.0.0.1 --port 8765
```

首次运行会：

1. 在项目内创建 `.venv`；
2. 安装 `requirements.txt`（Flask、Playwright、pytest）；
3. 执行 `playwright install chromium`（本机已有缓存则直接复用）；
4. 用 `.venv` 内解释器重新启动自身。

启动后打开 **http://127.0.0.1:8765/** 即为中文操作入口；
被测实验台在 **http://127.0.0.1:8765/lab/**。

页面按 1280×720 设计，关键操作（场景选择、`运行示例`、结果状态）在首屏可见，
证据详情可展开。

## 网页操作说明

- **运行示例**（按钮 id 为 `run-demo`）：用小样例依次运行
  一个正常场景（同名命令完整路径消歧并验证状态）和一个边界场景（禁用目标停止），
  目标 15 秒内结束，两条运行记录都保留可查看。
- **完整验收场景**：左侧“完整验收场景（按需运行）”分组，覆盖上下文切换、
  搜索重排、歧义停止、动态预设、快捷键冲突。
- **自定义任务**：在编辑框粘贴符合 `rpa.command-menu.v1` 的 JSON 后运行；
  协议违规会在启动阶段被拒绝并返回具体错误。
- 结果区每次运行展示：状态、耗时、逐步决策理由（为什么选中/为什么停止）、
  候选命令清单、DOM 观测值、截图与采集边界检查结果。
- 所有结果均来自真实 Playwright 执行，服务端不预填任何成功状态。

## 测试命令

```bash
# 方式一：入口自动引导环境后跑全部测试
python3 app.py --test

# 方式二：虚拟环境就绪后直接用 pytest
.venv/bin/python -m pytest tests/ -v

# 只跑不依赖浏览器的快速测试
.venv/bin/python -m pytest tests/test_protocol.py tests/test_resolver.py tests/test_isolation.py tests/test_webapi.py -v
```

端到端测试会真实启动 chromium 访问本地 Flask 服务，并把每个样例的执行结果与
`data/ground_truth` 中的独立真值逐项断言；其中包含“正常+边界 ≤ 15 秒”的预算检查，
以及“采集器源码不得出现固定序号 `.nth(` 点击”的源码守卫。

## 协议与解析库声明

- 任务协议：`rpa.command-menu.v1`，支持的动作子集为
  `open_path`、`search_commands`、`switch_context`、`snapshot_menu`、
  `expect_state`、`wait_visible`；完整子集约束见 [PROTOCOL.md](PROTOCOL.md)。
- JSON 解析使用 **Python 标准库 `json`**（CPython 自带的成熟开源实现，
  Python Software Foundation 维护），并通过 `object_pairs_hook` 启用重复键检测；
  不接受注释、尾随逗号、表达式、变量、循环或通配符。
- 浏览器自动化使用 **Playwright for Python**（Apache-2.0 开源），仅用 chromium。

## 采集边界（如何保证“不读真值代替采集”）

1. 采集器 `LabSession` 注册了 Playwright 路由守卫，只放行同源
   `/lab/`、`/lab/static/*` 与两个公开只读接口 `/lab/api/snapshot`、`/lab/api/health`；
   任何越界请求一律 abort 并写入证据。
2. `src/rpa` 源码不引用 `ground_truth` 字样、不导入 `sample_data`/`tests`，
   由 `tests/test_isolation.py` 做 AST/文本扫描强制保证。
3. 真值文件物理隔离在 `data/ground_truth/`，只有 pytest 读取。
4. 样例生成器与采集器是两个独立模块：生成器只写文件，采集器只经浏览器与公开接口工作。

## 虚构数据说明

设备（温控台/真空泵/光源）、实验上下文（ALPHA/BETA）、预设方案、禁用规则、
排序亲和度均为虚构；状态只存在于 Flask 进程内存与页面 DOM 中，重启即复位。
