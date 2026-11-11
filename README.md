# 公开标本目录 · 有界蓄水池抽样采集工具

对**规模预先未知、条目可能跨页重复**的公开标本目录进行抽样采集的可运行项目。
核心算法为可复现的**蓄水池抽样（Reservoir Sampling, Algorithm R）**；当请求预算在扫描
中途耗尽时，结论严格限定为“**已观察集合**”，不冒称全站无偏样本。全部数据为本地虚构生成，
不依赖真实账户、外部业务服务或 Docker。

## 1. 环境要求

- Python 3.11+（开发验证版本：3.11.6）
- 操作系统自带 `venv`/`pip`；首次安装依赖需要可访问 PyPI 的网络
- 浏览器通道（可选）：Playwright + Chromium

## 2. 一键启动（无需手动激活虚拟环境）

```bash
python3 app.py --host 127.0.0.1 --port 8765
```

入口脚本会自动完成：

1. 在项目下创建 `.venv` 并安装 `requirements.txt`（仅首次，约数十秒）；
2. 以该虚拟环境解释器重新执行自身；
3. 在后台线程启动本地虚构标本源站（默认 8766，被占用自动换端口）；
4. 启动中文 Web 入口，浏览器打开 `http://127.0.0.1:8765/` 即可操作。

可选参数：`--source-port <端口>`、`--no-source`（改用外部源站，配合环境变量
`SOURCE_BASE_URL`）、`SKIP_VENV_BOOTSTRAP=1`（跳过自举，使用当前解释器）。

## 3. 网页操作说明

- 左栏配置目录入口、随机种子、样本量 k、请求预算与采集通道（HTTP / Playwright 浏览器）；
- 右上角 **`id=run-demo` 的“运行示例”按钮**：依赖安装后依次运行
  “正常完整扫描”与“预算中途耗尽”两个小场景，本地实测约 0.6 秒（验收目标 ≤15 秒），
  两个结果都保留在任务列表中可切换查看；
- “运行选中验收场景”下拉框提供更完整场景：large 数据集完整扫描、large 预算耗尽、
  样本量大于总体、HTML + Playwright 浏览器采集；
- 右栏展示统计卡片、**结论声明、扫描边界（含未抓取的下一页 URL）、最终样本来源页/详情链接**；
- 折叠区展示**每一次入样/替换事件（随机数、槽位、被挤出 ID、事件后蓄水池）**、逐页扫描证据、
  原始 JSON，以及小总体多种子**频率检验**（含“不构成全站覆盖证明”免责声明）。
- 页面按 1280×720 设计，关键操作与结果首屏可见，其余证据折叠展开。

## 4. 支持的协议子集（明确声明）

| 通道 | 受支持形态 | 不支持（直接报错，不静默猜测） |
| --- | --- | --- |
| HTTP GET + JSON | 顶层对象含 `items:[{id, title?, detail_url?}]` 与不透明 `next_cursor`；**无 total 字段**，仅靠游标翻页 | XML、POST 翻页、鉴权接口、非 200 且不可重试的响应 |
| HTTP GET + HTML | `div[data-role=catalog]` 下 `article.specimen[data-id]`，`a.detail-link` 详情，`a[data-role=next-page]` 翻页 | 依赖 JavaScript 渲染的页面（请走浏览器通道） |
| Playwright/Chromium | 导航后读取渲染 DOM，复用同一 HTML 协议子集解析 | 未安装时给出明确中文安装提示，不隐式降级 |

解析库：`requests`（HTTP）、`BeautifulSoup4` 内置 `html.parser`（HTML）、`Playwright`（浏览器）。
请求预算口径：**每发起一次请求尝试（含重试）计 1 次**。

## 5. 安装依赖与测试命令

```bash
# 运行期依赖（app.py 自举时已自动安装；手动安装如下）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 测试依赖（含 pytest 与 Playwright）
.venv/bin/pip install -r requirements-dev.txt
python3 -m playwright install chromium   # 仅浏览器端到端测试需要

# 运行全部 42 个测试（无 Playwright 内核时浏览器用例自动 skip）
.venv/bin/python -m pytest -q

# 仅核心算法/频率检验（不需要网络服务以外的东西）
.venv/bin/python -m pytest tests/test_reservoir.py tests/test_frequency.py -q

# 重新生成独立真值（测试夹具也会在临时目录自动生成）
.venv/bin/python -m source_site.generator --out data/golden
```

## 6. 验收要求与对应测试/实现位置

| 验收要求 | 实现位置 | 独立断言测试 |
| --- | --- | --- |
| 重复条目不增加入样机会 | 扫描去重在抽样之前，唯一 ID 流才进入抽样器 | `tests/test_runner.py::test_duplicates_do_not_increase_selection_chance`（真实含重复目录 vs 真值唯一流，300 组种子逐 ID 频率对照） |
| 相同种子与输入顺序可复现 | 独立 `random.Random(seed)` 实例，记录每次随机数 | `tests/test_reservoir.py::test_reproducible_same_seed_same_order`（与独立参考实现逐元素比对） |
| 样本量大于总体 | 前 N 个直接填满，标记 `full_population_taken`，无替换 | `tests/test_runner.py::test_sample_size_greater_than_population` |
| 中途预算耗尽 | 预算用尽即停，记录未抓取的下一页，声明仅限已观察集合 | `tests/test_runner.py::test_budget_exhausted_declares_observed_only`、`tests/test_scanner.py::test_budget_*` |
| 小总体多种子频率检验 | 25 选 5、2000 次试验、Pearson 卡方（α=0.05 临界 36.415, df=24） | `tests/test_frequency.py`；网页折叠区可交互复算 |
| 不构成全站覆盖证明 | 频率检验与预算声明均显式否认全站覆盖 | 免责声明文案断言见 `test_frequency.py`、`test_e2e_http.py` |
| 采集器/生成器分离，真值仅测试用 | 静态扫描强制 `collector` 不引用源站/真值，仅允许 HTTP/浏览器/解析库依赖 | `tests/test_isolation.py` |
| 真实浏览器端到端 | Chromium 采集 HTML 目录 + 浏览器点击 `#run-demo` 验证 15 秒内出两结果 | `tests/test_e2e_browser.py`（5 个用例） |
| Web API 端到端 | 真实 WSGI 服务 + 真实 HTTP 请求，无 mock | `tests/test_e2e_http.py`（6 个用例） |

## 7. 目录结构

```
app.py                     # 统一入口：venv 自举 + 源站线程 + Web 服务
collector/                 # 采集器（只经公开接口/浏览器取数，被隔离约束保护）
  reservoir.py             # Algorithm R 蓄水池抽样（可复现、全事件记录）
  parsers.py               # JSON/HTML 协议子集解析
  scanner.py               # HTTP 扫描器（预算、重试、扫描边界事件）
  browser.py               # Playwright 扫描器（同预算口径，可注入浏览器）
  runner.py                # 去重→唯一 ID 流→抽样→来源证据与边界声明
source_site/               # 本地虚构源站与数据生成器（与采集器分离）
  generator.py             # 虚构标本、跨页/页内重复、独立真值导出
  server.py                # JSON/HTML 目录端点、详情页、故障注入
webapp/                    # Flask 中文操作入口
  app.py  task_manager.py  frequency.py
  static/                  # index.html / style.css / app.js
tests/                     # 42 个测试：单元、隔离、HTTP 与浏览器端到端
data/golden/               # 独立真值输出目录（git 忽略，仅供测试）
```

## 8. 重要边界（如实说明）

1. “无偏”只针对**实际观察到的唯一 ID 流**；完整扫描时覆盖该目录端点，预算耗尽时仅覆盖已扫页面，
   任何情况下都不自动外推到站点其他端点。
2. 频率检验是边际入样概率的实证检查（卡方），**不是全站覆盖证明**：未进入 ID 流的条目无法被检验发现。
3. 浏览器通道依赖本机 Chromium 内核；未安装时 Web 场景会返回具体安装指引错误，而不是伪造成功。
4. 源站故障注入（环境变量 `FAIL_MODE=http500:<页>` / `intermittent`）仅用于本地验证重试与错误展示。
