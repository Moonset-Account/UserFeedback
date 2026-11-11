# 类型化实验矩阵 RPA 录入系统

把结构化实验矩阵（TSV）通过 **Playwright 真实浏览器自动化** 录入一个本地
电子表格式控件（HTML/CSS/JS 页面），严格区分并保真五种语义值：

| 语义 | 编码/形态 | 保真要点 |
| --- | --- | --- |
| 标识符文本 `id_text` | `007`、`EXP-042` | 保留前导零，强制文本格式，不转数字 |
| 十进制数 `decimal` | `1.20`、`-2.75`、`0.0001` | 数值按 `Decimal` 比对，回读保留原始字面量（尾零） |
| 日期文本 `date_text` | `2026-11-09` | 按字符串原样保存，不转 Date 对象 |
| 普通文本 `text` | 可含制表符、换行、回车、`=1+1` | 逐字符保存；公式样式只作文本，不求值 |
| 空字符串 `empty` | `""` | 显式空串，与缺失严格区分 |
| 缺失值 `missing` | 行尾截断 / `<NA>` | 不触碰目标格，回读时目标格仍为缺失 |

目标控件具备：冻结列、只读格、隐藏列、两行合并分组标题。
自动化**只按可见列标题定位数据区**，使用真实点击、键盘逐格输入与
`insertText` 粘贴输入，不以 JS 直接给数据模型赋值。

> 本项目是一个聚焦的 RPA 录入演示，**不是通用表格管理系统**：
> 不做多表、公式引擎、协作或持久化编辑能力。

## 一、快速开始

要求 Python 3.11+。统一入口会自动创建并使用项目内 `.venv`，无需手动激活：

```bash
python3 app.py --host 127.0.0.1 --port 8765
```

首次启动自动完成：创建虚拟环境 → 安装 `requirements.txt` →
校验/安装 Playwright Chromium → 生成虚构样例数据。
看到 `[ready] 打开 http://127.0.0.1:8765/ 即可操作` 后，浏览器打开根路径。

页面（适配 1280×720，关键操作首屏可见）：

- **`运行示例` 按钮（id=`run-demo`）**：小样例依次运行「正常 demo-normal」
  与「边界 demo-edge」两个场景，目标 15 秒内结束，两份结果都保留可查看。
- **更完整验收场景**：下拉选择 `acceptance-full` 后单独运行。
- 任务结果区展示：成功/拒绝/失败/跳过计数、来源证据（接口 URL、字节数、
  SHA-256 指纹、截图）、逐格状态与具体差异、可按状态筛选。

## 二、依赖安装（手动方式）

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

依赖：`Flask>=3.0`、`playwright>=1.44`。无 Docker、无外部业务服务、无真实账户。

## 三、测试命令

```bash
# 全部 41 个测试（含真实浏览器端到端，约 30 秒）
python3 -m pytest -q

# 仅纯逻辑单元测试（秒级，不启动浏览器/服务）
python3 -m pytest tests/test_core_logic.py -q

# 独立真值断言 + 采集边界（自管 Flask 子进程）
python3 -m pytest tests/test_truth_assertions.py -q

# Playwright 端到端（含 15 秒时限断言）
python3 -m pytest tests/test_e2e_playwright.py -q
```

测试分三层：

1. `tests/test_core_logic.py`：TSV 协议解析、五值建模、危险公式判定、回读比对。
2. `tests/test_truth_assertions.py`：以**独立真值** `truth.json` 为期望，
   逐格核对建模与规划；并断言 `truth.json` 不通过任何 HTTP 接口暴露（403）。
3. `tests/test_e2e_playwright.py`：真实 Chromium 跑采集→规划→录入→回读全链路，
   断言计数与真值一致、无 failed、两演示场景浏览器总耗时 < 15s。

## 四、声明支持的协议子集

输入为 **UTF-8 的 TSV**（RFC 4180 双引号子集），解析器见
`rpa_grid/tsv_parser.py`：

- 分隔符：水平制表符 `\t`（兼容逗号 CSV，显式传 `delimiter=","`）。
- 双引号字段内可包含：制表符、换行（`\n`/`\r\n`/`\r`）、转义双引号（`""`）。
- 自动剥离 UTF-8 BOM；接受三类行尾；末尾单个换行不产生空行。
- 空字符串：相邻分隔符之间的空字段；缺失：行尾截断或显式 `<NA>` 标记。
- **不支持**：类型自动推断、公式求值、自定义转义字符、多字符分隔符、非 UTF-8 编码。

TSV 布局约定：第 1 行可见列标题，第 2 行列类型声明（`id_text`/`decimal`/
`date_text`/`text`），第 3 行起为数据。

## 五、架构与采集边界

```
app.py                     统一入口（自举 venv、参数 --host/--port）
rpa_grid/
  tsv_parser.py            RFC4180 子集解析器（纯函数，可单测）
  value_model.py           五值类型模型、危险公式判定、回读比对
  matrix.py                类型化矩阵 + 网格结构配置
  sample_generator.py      样例/真值/配置生成器（与采集器分离）
  collector.py             采集器：仅经登记的 HTTP 接口/file 注入，字节保真
  planner.py               规划器：按可见标题定位、只读/隐藏拒绝、策略选择
  automation.py            Playwright 引擎：真实交互 + 回读校验
  runner.py                任务编排与结果落盘
  webapp.py                Flask 路由与中文页面 API
templates/ static/         控制台页面 + 目标电子表格式控件
tests/                     三层测试
data/samples/<场景>/        matrix.tsv / schema.json / grid_config.json /
                           scenario.json / truth.json（仅测试，不对外）
data/runtime/              任务结果、计划、截图（运行期产物）
```

**采集边界（强约束）**：

- 采集器 `collector.py` 只允许同源 HTTP：
  `GET /samples/<场景>/matrix.tsv` 与 `grid_config.json`、`schema.json`；
  测试可用 `file://` 注入，其余 scheme 直接拒绝。
- 字节读取后 UTF-8 解码，禁用 Python 通用换行，保证裸 `\r` 不被改写。
- `truth.json` 不注册任何数据路由，`/samples/<场景>/truth.json` 返回 **403**；
  采集器源码中不含任何对 truth 的引用。真值仅供 `tests/` 读取比对，
  不能代替采集。
- 样例全部为虚构数据，由 `sample_generator.py` 生成，与采集器职责分离。

## 六、保真与拒绝策略（验收点对应）

- **前导零**：输入前置格格式为 `text` + 类型提示 `id_text`，键盘逐字符输入，
  回读断言仍为 `0001`/`id_text`。
- **跨只读格粘贴**：控件对粘贴区域预检，命中任意只读格则**整单取消、零写入**
  并弹错误条；规划阶段也会把只读格标记为 `rejected` 并给出解释。
- **制表符/换行文本**：打开模态多行编辑器，`insertText` 真实粘贴（系统剪贴板
  会把裸 `\r` 规范化成 `\n`，故不用剪贴板路径），提交前后两次逐字符校验。
- **危险公式样式**（`=1+1`、`=cmd|...`、`@SUM(..)`、`+1-2`）：先锁文本格式
  再输入，回读确认仍为文本、未被求值；普通负数（`-2.75`）不误判为公式。
- **无法保真即拒绝**：任一格失败返回 `failed`/`rejected` 并写明原因，
  整体 `ok` 只取决于是否存在 `failed`；其他格的成功不会掩盖失败，
  网页与结果 JSON 均逐格独立呈现。

## 七、已知边界（如实说明）

- 目标控件是为演示实现的内存态网格，刷新页面后数据重置（任务结果与截图已落盘）。
- 仅实现声明的 TSV/CSV 子集与六种语义类型，不追求通用电子表格能力。
- 无头模式为默认；需要肉眼观察可在页面取消「无头模式」勾选后运行。
