# 响应语义判定器（采集前分类）

在 HTTP 200（或其他状态码）下区分源站返回的 **有效资料 / 合法空结果 / 维护页面 /
访问提示 / 错误页 / 模板残缺页**，并把证据不足或类别歧义的响应 **隔离为不确定**，
支持用户补充规则后对隔离项 **复判**（旧判断与新依据同时保留）。

判定 **不依赖单一状态码、正文长度或某个关键词**，而是组合四层信号：

| 信号层 | 依据 |
| --- | --- |
| 结构契约 structure | 地标区域（header/nav/breadcrumb/main/footer）、标签闭合栈、截断声明、骨架占位占比 |
| 明确标记 marker | 空结果/维护/访问/错误的多语言（zh/en/ja）标记，且要求出现在对应专用区域，偶然命中只作反证 |
| 内容特征 content | 资料卡片、表格数据行、实质文本、空状态区域 |
| 导航来源 navigation | Referer 与页面 `meta[x-nav-source]` 声明的来源链路（仅弱权重解歧，不单独定案） |

每次判定输出逐条证据（含区域与原文片段）、各类别加权得分、置信度、
置信边界（置信度 ± 前名分差）与隔离原因。

## 支持的协议子集

- HTTP/1.1 + TLS 由 `requests`（采集）与 Flask/werkzeug（源站）实现；
  本项目只使用 **HTTP GET 文本响应**（HTML），不涉及 POST 写入、鉴权会话、
  二进制下载、WebSocket；
- 本地文件模式：读取样例生成器导出的 HTML 文件；
- 浏览器模式：Playwright 无头 Chromium，导航方式仍为 GET；
  判定使用 `response.body()` 的**线路原始 HTML**（浏览器自动补全的渲染后 DOM 仅作对照），
  以保留未闭合标签/截断等结构事实。

## 目录结构

```
app.py                     统一启动入口（自动切换 .venv，挂载控制台 + 虚构源站）
classifier/                判定引擎（纯函数，无网络依赖）
  models.py                类别枚举、证据/结果数据模型、置信边界参数
  markers.py               多语言标记词表与区域 hint
  features.py              页面特征提取（地标、区域命中、标签栈、骨架占比）
  custom_rules.py          用户补充规则的校验与匹配
  engine.py                加权打分、硬门控、置信边界、隔离逻辑
collector/                 采集器（http / file / browser 三模式）
source_site/               本地虚构源站与样例生成器（数据全部虚构）
  catalog.py               公开场景目录（不含期望分类，控制台唯一可用的源站模块）
  scenarios.py             场景注册表 + 独立真值（仅 tests/ 与导出可用）
  templates.py             21 个语义页面的 HTML 构造（含全部陷阱场景）
  server.py / export.py    源站服务 / 静态样例导出
web/                       中文控制台（HTML/CSS/JS）
runner.py                  任务运行与落盘、复判历史链
webapp.py                  控制台 Flask API
tests/                     49 个测试（独立预期断言 + 架构边界 + 端到端）
runtime/                   运行期自动生成（任务、规则、样例、真值、截图；已 gitignore）
```

**真值隔离机制**：采集器与判定引擎不 import `source_site`、不读取真值文件
（由 `tests/test_architecture.py` 以 AST 强制）；测试中的独立预期表 `EXPECTED`
手写在 `tests/conftest.py`，真值文件只用于交叉核对预期表本身。

## 安装依赖

需要 Python 3.11+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt          # 运行依赖
.venv/bin/python -m pip install -r requirements-dev.txt      # 测试依赖（含 pytest、Playwright）
.venv/bin/python -m playwright install chromium               # 浏览器模式（可选，本机有缓存则秒完）
```

不使用浏览器模式时，只需 `requirements.txt`。

## 启动

统一入口，**无需手动激活虚拟环境**（检测到 `.venv` 会自动以其解释器重执行）：

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 即中文控制台；虚构源站目录在 `/source/`，
样例页面在 `/scenarios/<id>`。首次启动会自动导出样例与真值到 `runtime/`。

页面在 1280×720 下关键操作与结果首屏可见，详细证据可展开。右上角
**`运行示例`（id=`run-demo`）** 按钮依次运行一个正常场景（有效资料页）和一个边界场景
（中文合法空结果），实测 1 秒内完成（验收要求 15 秒内），两个结果保留可查；
**运行完整验收** 按钮跑全部 22 个场景（含连接重置导致的采集失败）。

## 典型操作

1. 左栏选择采集模式（HTTP / 浏览器 / 文件），填入目标（可用场景下拉快速填入）；
2. 运行单次任务，右栏查看分类徽章、置信边界与逐条证据；
3. 被隔离的任务（不确定）在列表中有「隔离」标记；在左栏规则编辑区写入补充规则并保存
   （JSON，非法规则会被拒绝并提示原因）；
4. 选中隔离任务点击 **用当前规则复判**：不重新采集，复用 HTML 快照重跑判定，
   历史链保留 v1 旧判断并追加 v2 新依据、命中规则与变更轨迹。

规则文件格式：

```json
[
  {
    "code": "rule-busy-001",
    "name": "忙碌/未就绪页面识别",
    "phrase": "正在为您检索相关资料",
    "region_contains": "pending-tip",
    "category": "BROKEN_TEMPLATE",
    "weight": 0.6
  }
]
```

`phrase` 为正则（忽略大小写）；`region_contains` 可选，要求命中元素祖先链的
id/class 包含该 token；`weight` 允许 ±0.8，负值可用于反对某类别。

## 测试

```bash
.venv/bin/python -m pytest          # 49 个用例，约 22 秒（含 Playwright 真机用例）
```

测试分层：

- `test_engine_classification.py` 独立预期 vs 实际判定，覆盖验收混淆场景：
  含“维护”正文不误判、相似模板错误页、多语言空结果、截断/未闭合/骨架残缺、
  歧义页隔离、500 状态码下的维护语义、合法空结果与采集失败分离；
- `test_collector.py` 三种采集模式、传输故障、HTTP 与浏览器结论一致性；
- `test_rules_and_reclassify.py` 规则校验、区域失配不命中、复判历史保留；
- `test_architecture.py` AST 级边界：判定/采集层禁止依赖源站与真值、引擎无网络调用；
- `test_web_e2e.py` Flask 测试客户端端到端：示例 15 秒内完成、八类全覆盖、复判流。

仅跑离线引擎用例（无需浏览器）：

```bash
.venv/bin/python -m pytest tests/test_engine_classification.py tests/test_architecture.py
```

## 验收场景与预期对照

| 场景 id | 预期分类 | 验收关注点 |
| --- | --- | --- |
| normal / normal-keyword | VALID | 正常页；后者正文含多处“维护”及维护标记词条但区域不符，不得判维护 |
| empty-zh / empty-en / empty-ja | EMPTY | 多语言合法空结果，与 FETCH_FAILED 严格分开 |
| maintenance-zh/en/ja | MAINTENANCE | 维护专用区域 + 多语言标记 |
| maintenance-500 | MAINTENANCE | HTTP 500 下语义仍以页面为准 |
| access-login/permission/captcha-zh、access-en/ja | ACCESS_NOTICE | 登录/权限/验证三类门禁 |
| error-zh / error-en | ERROR_PAGE | 与正常模板同骨架的 200 错误页 |
| broken-truncated / broken-skeleton / broken-unclosed | BROKEN_TEMPLATE | 截断声明 / 骨架占比 / 未闭合标签 |
| ambiguous | UNKNOWN | 空标记与资料卡片互斥，硬隔离 |
| uncertain-busy | UNKNOWN | 信号稀薄、置信不足隔离，可经规则复判 |
| tcp-reset | FETCH_FAILED | 连接重置，传输失败不进入语义判定 |

## 虚构数据声明

源站“星港资料馆”的机构名、文档、文案、错误编号均为虚构，不依赖真实账户、
外部业务服务或 Docker；全部内容由项目本地生成。
