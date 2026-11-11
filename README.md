# 曲线编辑器 RPA（本地曲线校准点自动录入）

通过 Playwright 操作一个**受控本地曲线配置页**，把给定校准点录入页面。页面只允许
**鼠标拖动控制点**与**键盘方向键微调**，并内置缩放/平移、反向纵轴、网格吸附与
“控制点 x 严格递增、不可交叉”约束。RPA 不实现任何绘图引擎或通用图形编辑器，
只做“建映射 → 操作 → 读页面数值 → 迭代修正 → 裁决”的自动化闭环。

## 快速开始

要求 Python 3.11+（在 3.11.6 实测）。统一入口会自动创建 `.venv`、安装依赖并准备
Playwright Chromium，**无需手动激活虚拟环境**：

```bash
python3 app.py --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765/> 即是中文操作控制台：

* 左栏选择任务（快速演示 / 完整验收两组）、选择无头或有头浏览器；
* 点击 **运行示例**（id=`run-demo`）会依次跑一个正常场景与一个边界（越界失败）场景，
  实测约 7 秒结束（要求 15 秒内），两条结果都保留在“执行记录”；
* 右栏展示裁决状态、逐点目标/实际/误差、每次迭代明细、坐标映射、来源证据、截图与具体错误；
* 页面按 1280×720 设计，关键操作与结果首屏可见，其余证据折叠展开。

若自动建 venv 失败但系统 Python 已装好依赖，入口会回退到系统解释器继续运行。

## 安装与测试命令

```bash
# 依赖（入口脚本通常会自动执行以下步骤）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

# 全部测试（纯逻辑单元测试 + 协议/边界测试 + 真实浏览器端到端测试）
python3 -m pytest tests/ -q

# 只跑不依赖浏览器的快速测试
python3 -m pytest tests/test_geometry.py tests/test_samples_contract.py -q

# 重新生成虚构样例与独立真值（幂等）
PYTHONPATH=src python3 -m curve_rpa.sample_gen
```

端到端测试会用标准库 `wsgiref` 多线程服务器在随机端口起一个真实源站，通过 HTTP 与
Playwright 走完整链路；本机缺少 Chromium 时相关用例自动 skip。

## 支持的协议子集（成熟开源库）

“解析协议”仅使用成熟开源生态的 **HTTP/1.1 + JSON** 子集，不自定义二进制协议：

| 层次 | 选型 | 子集范围 |
| --- | --- | --- |
| 传输 | `requests` 2.x（采集器）/ Flask 3 + `wsgiref`（源站） | 仅 HTTP/1.1，本地回环，GET 采集、控制台 POST 触发；无重定向、无鉴权、无 Cookie、无压缩依赖 |
| 文档 | 内置 JSON 解析（标准库 + requests） | `object/array/number/string/boolean`、`required`、`enum`、`minimum/maximum`、`const`（JSON Schema draft-07 子集），版本字段 `schema_version="curve-task/v1"` |
| 浏览器 | Playwright 1.58（Chromium） | 鼠标拖动、键盘按键、DOM 读数与截图；不调用任何页面内部私有状态 |

## 核心工作原理

1. **公开接口采集**：`HttpTaskCollector` 只用 GET 访问 `/api/tasks/<id>` 取任务规格，
   不读本地任务文件，更不读真值目录。
2. **依据公开刻度建立坐标映射**：读取编辑器 SVG 上可见的 `.tick-x` / `.tick-y`
   刻度标签数值及其像素位置，最小二乘拟合一维仿射映射 `pixel = slope·value + intercept`。
   因为映射完全来自公开刻度，非默认缩放、平移、反向纵轴（斜率符号变化）都自适应，
   视口与 SVG 用户坐标的偏移也显式换算。
3. **操作与迭代修正**：
   * 第 1 次尝试按映射算出目标像素，真实拖动控制点，页面在释放时做网格吸附；
   * 之后读右侧“页面显示值”，按误差用方向键微调。每轴先按一次“增大键”试探，
     下一轮依据页面读数的真实变化学会键位方向（因此不预设反向纵轴的键义），
     再单向逐格逼近，避免越过目标；
   * 检测停滞（被轴域/顺序约束夹住）与周期 2 振荡（网格正中间目标的物理极限），
     保留观测到的最优页面读数。
4. **基于数值误差裁决，而非像素**：达标判定只比较页面显示值与目标值的误差是否在容限内；
   无法达标时给出明确失败分类：
   * `FAIL_OUT_OF_BOUNDS`：目标超出轴域，页面只能夹到边界；
   * `FAIL_ORDER`：目标点 x 不满足严格递增，页面阻止两点交叉；
   * `FAIL_TOLERANCE_UNREACHABLE`：网格吸附下界（或顺序夹取）使误差无法进入容限，用尽最大尝试次数。
5. **页面叠加证据**：RPA 在编辑器 SVG 上叠加红色目标叉、绿色实际点虚线圈，
   右上角面板逐次显示每个点的 Δx/Δy 与阶段；截图与 `result.json` 落盘到 `data/runs/<task_id>/`。

## 验收场景覆盖

| 任务 | 关键验收点 | 预期裁决 |
| --- | --- | --- |
| `demo-normal` | 非默认缩放 1.35 + 平移；目标落在两条网格线正中间（吸附误差 2.5，容限 3.0） | `SUCCESS` |
| `demo-boundary` | 反向纵轴；目标 y=118 越界，页面夹到 100 | `FAIL_OUT_OF_BOUNDS` |
| `accept-order` | 两个点目标 x 顺序反转，页面强制最小间隔、禁止交叉 | `FAIL_ORDER` |
| `accept-strict-tol` | 网格中间目标 + 容差 0.1 小于可达吸附误差 2.5 | `FAIL_TOLERANCE_UNREACHABLE` |
| `accept-inverted` | 缩放 1.5 + 平移 + 反向纵轴 + 网格中间目标，三点综合 | `SUCCESS` |

网格正中间目标（如 32.5，步长 5）在页面上有 30 与 35 两个等距最优吸附值，真值同时记录
两个候选，端到端断言按“实际值属于候选集合且误差幅度一致”校验，不硬编码单侧结果。

## 目录结构与职责分离

```
app.py                     统一入口（自动建 .venv / 装依赖 / 装 Chromium / 启动服务）
requirements.txt
pytest.ini
src/curve_rpa/
  geometry.py              仿射映射拟合、网格吸附、越界/顺序判定（纯函数，独立可测）
  task_schema.py           curve-task/v1 公开协议子集的校验与编辑器视图
  sample_gen.py            样例生成器：虚构任务 + 独立真值（与采集器分离，可独立运行）
  collector.py             采集器：只走公开 HTTP 接口，禁止任何本地文件/真值访问
  engine.py                Playwright RPA：刻度映射、拖动/微调迭代、叠加层、裁决
  web.py                   本地源站 + 控制台 API（多线程 WSGI）
static/editor/index.html   受控曲线编辑器（被自动化对象，无数值输入、无通用绘图能力）
static/console/index.html  中文控制台（配置、运行、证据与错误查看）
data/tasks/                生成器产出的虚构任务（公开接口的数据源）
data/truth/                独立真值，仅供测试断言；采集器禁入，源站也不暴露该路径
data/runs/                 真实执行产物（result.json、final.png），git 忽略
tests/                     独立预期断言 + 端到端验证
```

**采集边界**：采集器与引擎源码不含任何真值/任务目录路径（有静态测试强制校验）；
真值只能由测试读取，用于核对真实执行结果，不能替代采集。源站对 `data/truth`
没有任何路由，外部访问得到 404。

## 数据声明

所有任务、轴域、初始点、目标点均为虚构数据，由 `sample_gen.py` 生成，
不依赖真实账户、外部业务服务或 Docker。
