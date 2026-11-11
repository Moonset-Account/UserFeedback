# 科研测量页面语义契约采集器

面向科研测量页面（样品干重、粗蛋白含量、含水率等台账）的采集器。页面改版时即使
**CSS 选择器完全保留**，只要把“干重”改成“湿重”、把“平均”改成“中位数”、或把单位
倍数从 `g` 改成 `mg`，采集器都会依据 **字段标签 + 单位 + 限定说明 + 数据类型**
组成的语义契约拦截差异，把受影响记录隔离，而不是仅因选择器命中就继续发布。

所有数据均为虚构，不依赖真实账户、外部业务服务或 Docker。

---

## 1. 目录结构

```
attempt-001/
├── app.py                  # 统一 Web 入口（Flask），启动命令见下
├── requirements.txt
├── pytest.ini
├── collector/              # 采集器（与样例生成器分离，不读取任何真值）
│   ├── contracts.py        # 字段语义契约、版本链、批准派生
│   ├── units.py            # 受限单位子集解析与倍数换算
│   ├── checks.py           # 标签/单位/限定说明/数据类型 四类语义检查
│   ├── browser.py          # Playwright(Chromium) 浏览器采集驱动与原始证据
│   ├── engine.py           # 采集→检查→隔离/发布→批准→重采 主流程
│   └── storage.py          # 运行记录、契约版本、批准记录本地持久化
├── site_gen/
│   └── generator.py        # 样例源站页面 + 独立真值生成器（虚构数据）
├── templates/index.html    # 中文操作界面
├── static/                 # 界面 CSS / JavaScript
├── tests/                  # 独立预期断言 + 单元测试 + 真实浏览器/HTTP 端到端
├── generated/              # 运行后生成：samples/ 页面 与 truth/ 独立真值（已 gitignore）
└── data/                   # 运行后生成：runs/contracts/approvals（已 gitignore）
```

## 2. 支持的协议子集（明确声明）

**单位子集**（`collector/units.py`，子集外单位直接拒绝、不做静默换算）：

| 量纲 | 支持单位 | 基准单位 |
| --- | --- | --- |
| 质量 | `g`、`mg`、`kg`（含中文别名“克/毫克/千克”） | `g` |
| 质量分数 | `%`（含 `wt%`、全角 `％`）、`g/g`、`mg/g` | `g/g` |

**选择器子集**（Playwright CSS 引擎）：id/class/标签组合（如 `table#measure`、
`th.col-dry`）与后代组合符（如 `table#measure tbody tr`）；不使用 XPath 或文本伪类。

**语义契约四要素**：允许的字段标签（子串匹配）、期望单位、必要限定说明与统计口径
（`mean`/`median`）、数据类型（`number`/`text`）。

## 3. 安装与启动（无需手动激活虚拟环境）

要求 Python 3.11+。

```bash
# 1) 安装依赖
python3 -m pip install -r requirements.txt

# 2) 安装 Playwright Chromium（若本机尚无浏览器缓存）
python3 -m playwright install chromium

# 3) 统一启动方式（端口可改）
python3 app.py --host 127.0.0.1 --port 8000
```

启动时自动生成样例站点并打开所需环境；浏览器访问 **http://127.0.0.1:8000/** 即可操作，
无需另外手动激活虚拟环境（`app.py` 会把项目根目录加入 `sys.path`）。

### 界面操作

- **`运行示例` 按钮（id=`run-demo`）**：依次运行一个正常场景（baseline）和一个边界
  场景（label 标签含义改变），实测约 2 秒完成（目标 15 秒内），两个结果均保留可查。
- **验收场景下拉框**：七个完整场景的选择入口（见第 5 节）。
- **自定义 URL**：可填写任意页面地址，采集只经浏览器公开交互进行。
- 详情区六个页签：**契约差异 / 原始证据（截图+HTML 片段+元信息）/ 隔离记录 /
  正式结果 / 批准与重采 / 契约版本与批准记录**。
- 页面按 1280×720 适配，关键操作与结果总览在首屏，其余证据可展开。

## 4. 测试命令

```bash
# 全部 52 项测试（单元 + 真实 Chromium 端到端 + 真实 HTTP 全链路），约 13 秒
python3 -m pytest

# 仅单元测试（秒级，不启动浏览器服务）
python3 -m pytest tests/test_units.py tests/test_checks.py tests/test_generator.py

# 仅真实浏览器端到端（file:// 页面，对照独立真值断言）
python3 -m pytest tests/test_e2e_browser.py

# 仅 HTTP 全链路（真实 Werkzeug 服务 + 浏览器访问公开 URL，含 15 秒演示断言）
python3 -m pytest tests/test_http_e2e.py
```

测试使用系统临时目录，不污染仓库 `data/`、`generated/`。

## 5. 验收场景与预期行为

选择器在全部场景中保持不变，仅语义/样式变化（真值见 `generated/truth/*.json`）：

| 场景 key | 变化内容 | 预期违规码 | 正式/隔离记录 |
| --- | --- | --- | --- |
| `baseline` | 基线页面，全部符合 v1 契约 | 无 | 4 / 0 |
| `restyle` | **纯样式改版**：仅 CSS 与装饰文案变化 | **无（不得误报）** | 4 / 0 |
| `label` | 干重→**湿重**（标签含义改变） | `LABEL_MISMATCH` | 0 / 4 |
| `unit` | `g`→**`mg`**，数值按 1000 倍改写 | `UNIT_SCALE`（证据含换算倍数 0.001） | 0 / 4 |
| `no-qual` | 列头去掉“**平均**”限定说明 | `QUALIFIER_MISSING` | 0 / 4 |
| `median` | 平均→**中位数** | `STAT_MISMATCH` | 0 / 4 |
| `type-error` | 某行数值单元格变成文本“仪器离线” | `TYPE_MISMATCH`（记录级） | 3 / 1 |

另有单位量纲不兼容（`UNIT_INCOMPATIBLE`）、子集外单位（`UNIT_UNSUPPORTED`）、
单位缺失（`UNIT_MISSING`）、列头选择器未命中（`HEADER_MISSING`）由单元测试覆盖。

## 6. 隔离与批准语义

- **未批准不发布**：列头级违规隔离该列涉及的全部记录；记录级违规只隔离对应行。
  `published` 与 `quarantined` 严格分离，正式结果中不可能出现未批准的语义数据。
- **批准生成不可变新版本**：用户在“批准与重采”页签勾选字段并填写审核人/理由后，
  生成 `v2`、`v3`…（`parent_version` 指向上一版），未批准字段的规则保持不变。
- **旧规则与受影响范围永久保留**：`data/approvals/approval-NNN.json` 记录旧字段规则、
  新规则、受影响的运行 ID、字段、行索引与记录数量。
- **重采验证**：用新版本契约重新采集，通过后记录才进入正式结果。

## 7. 采集器与真值的隔离

- 样例生成器（`site_gen/`）产出 `generated/samples/`（公开页面）与
  `generated/truth/`（独立预期真值）。
- 采集器（`collector/`）与 `app.py` 的代码中不存在任何对 `truth` 目录的引用
  （由 `tests/test_http_e2e.py::test_collector_code_never_reads_truth_directory` 强制检查）。
- 采集只通过 Playwright 浏览器导航与 DOM 读取、或 Flask 公开 HTTP 接口进行；
  真值仅供测试断言阶段显式读取。

## 8. 本地数据说明

- `data/runs/run-xxxx/`：`result.json`（完整结果）、`screenshot.png`（整页截图）、
  `table.html`（表格原始 HTML）。
- `data/contracts/vN.json`：已批准契约版本；`v1` 为内置基线，不写盘。
- `data/approvals/approval-NNN.json`：批准记录（旧规则 + 受影响记录范围）。
- 需要重置时直接删除 `data/` 与 `generated/` 即可，重启应用会重新生成样例。
