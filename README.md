# 公开实验数据导出地址采集器（虚构数据本地演练）

本地页面提供**短时有效、一次性**的实验导出链接和**稳定业务版本号**。链接可能过期、已被
使用，或返回 **HTTP 200 的错误 HTML**。本采集器在明确识别链接失效后，重新从页面取得
地址并核对业务版本；刷新不会改变当前采集对象，版本变化时立即停止；刷新有硬上限，不会
无限重试。全程不涉及登录或付费资源，所有数据均为虚构，由项目自带生成器产生。

## 一、快速开始

要求 Python 3.11+，无需 Docker，不依赖任何真实账户或外部业务服务。

```bash
# 1. 安装依赖（首次）
python3 -m pip install -r requirements.txt
#    测试还需要 pytest（开发依赖）
python3 -m pip install -r requirements-dev.txt
# 2. 如需浏览器自动化（JS 渲染页面），再安装 Chromium
python3 -m playwright install chromium
# 3. 统一入口启动（首次会自动创建 .venv 并安装依赖，之后自动复用，无需手动激活）
python3 app.py --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765/> 即可操作。启动后同进程还会在 `端口+1`（默认 8766）启动
本地公开源站。想换端口：`python3 app.py --host 127.0.0.1 --port 9000`（源站自动变为
9001，也可用 `--source-port` 显式指定）。

页面操作：

- **运行示例**（按钮 id=`run-demo`）：自动依次运行 1 个正常场景 + 1 个边界场景，
  实测约 1 秒完成（预算 15 秒内），两个结果都保留可查看；
- **运行采集**：在左侧选择完整验收场景（8 个）与协议子集后执行；
- 右侧首屏显示状态摘要与每次运行的尝试/刷新次数，点击卡片可展开**来源证据、事件时间线、
  三层校验明细与具体错误**；页面不预填任何成功结果，全部来自真实执行。

## 二、运行测试

```bash
# 全部用例（25 个：纯单元 + HTTP 引擎端到端 + Web 端到端 + Playwright 浏览器）
python3 -m pytest -q
# 仅不依赖浏览器的用例
python3 -m pytest -q -m "not playwright"
```

测试通过 HTTP 与源站交互（与采集器同一公开面）；`runtime_data/ground_truth/` 中的
独立预期**只允许测试读取**，采集器代码路径从不访问它。

## 三、支持的协议子集

| 后端 | 支持 | 不支持 |
| --- | --- | --- |
| HTTP 子集（`requests` + `BeautifulSoup4/lxml`） | HTTP/1.1 GET；HTML 4/5 中 `a[href]` 锚点解析；同源 `application/json` 清单；`application/zip` 二进制下载；状态码/响应头原样保留 | 不执行 JavaScript；不做站外跳转写入；不处理登录态 |
| Playwright 浏览器子集（Chromium 无头） | 导航后等待 JS 注入 DOM；读取渲染后锚点、版本号与实验编号；浏览器同源 request 上下文下载 | 仅 Chromium；不模拟人工登录/付费/验证码 |

## 四、失效识别与有限刷新策略

1. **取得地址**：打开实验页面，读取 `a#download-link` 锚点、稳定版本号与实验编号，
   保留页面 URL、状态码、锚点 HTML 等来源证据；
2. **失效识别（明确证据）**：
   - HTTP 410/404 → 链接过期或一次性链接已消费；
   - HTTP 200 但响应体是 HTML（魔数/Content-Type 判定）→ 错误 HTML；
   - 传输异常或其他非成功状态码 → 可刷新的下载失败；
3. **有限刷新**：仅在明确失效后重新从页面取址，默认最多刷新 **3 次**（页面可调，
   0–10），达到上限即停止并说明，绝不无限重试；
4. **版本守卫**：每次刷新后比对版本号与实验编号，任一变化立即停止（不下载新对象），
   说明旧版本与新版本；
5. **三层内容校验**（仅对 HTTP 200 的非 HTML 内容执行，短路返回）：
   - 类型：ZIP 魔数（`PK\x03\x04`）+ Content-Type，拦截 200 错误 HTML；
   - 结构：ZIP 完整性/CRC、必需成员（`manifest.json`、`readings.csv`、`notes/README.txt`）、
     manifest 可解析、成员 SHA-256 校验和、CSV 表头与数据行；
   - 业务身份：包内 `experiment_id` 与 `version` 必须与页面声明一致；
   - 结构/身份失败属于**终端错误**（刷新无法修复内容问题），不触发重试。

## 五、场景清单（生成器虚构，均为确定性场景）

| 场景 | 预期结果 | 尝试/刷新 |
| --- | --- | --- |
| `normal` 一次性链接直接成功 | success | 1 / 0 |
| `pre_expired` 下载前已过期，刷新后成功 | success | 2 / 1 |
| `error_html` HTTP 200 错误 HTML，刷新后成功 | success | 2 / 1 |
| `already_used` 一次性链接已使用（410），刷新后成功 | success | 2 / 1 |
| `persistent_expired` 持续失效，达到刷新上限停止 | failed_expired | 4 / 3 |
| `version_change` 刷新后版本 3.4.2→3.4.3，停止 | failed_version_changed | 1 / 1 |
| `identity_mismatch` 包内实验编号不符，终端失败 | failed_identity | 1 / 0 |
| `js_page` JS 渲染页面，仅 Playwright 可采集 | success | 1 / 0 |

## 六、目录结构与职责分离

```
app.py                     统一入口（venv 自举 + 生成数据 + 源站/采集站启动）
generator/                 样例生成器：虚构 ZIP、场景定义、独立真值 ground_truth
source_site/               本地公开源站（Flask）：短时链接状态机、错误页、版本变化
collector/                 采集核心：引擎、HTTP/Playwright 后端、三层校验（不读真值）
webapp/                    中文网页（配置、运行、证据与错误展示）
tests/                     独立预期断言 + 端到端验证
runtime_data/              运行期产物（generated/ground_truth/downloads，已 gitignore）
```

- **采集器与生成器分离**：采集器只经源站公开页面、同源 JSON 或浏览器 DOM 交互，
  不读取 `generated/`、`ground_truth/` 或源站 `/admin/*` 接口；
- **不可信数据处理**：下载物按不可信数据对待，只做字节级只读解析（ZIP/JSON/CSV），
  从不执行其中内容；可用 `python3 -I` 隔离模式复核；
- 源站 `/admin/reset` 仅用于演示/测试的确定性重置，不属于采集接口。

## 七、验收要求对照

- 下载前过期、200 错误 HTML、刷新后版本变化、一次性链接成功：见上表与
  `tests/test_engine_http.py`、`tests/test_engine_playwright.py`；
- 有效内容同时通过类型、结构、业务身份校验：`collector/validation.py` +
  `tests/test_validation.py`（8 个独立用例）；
- 版本变化停止并说明、不能无限刷新：`version_change` 与 `persistent_expired` 场景，
  引擎硬上限与事件 `version_changed`/`refresh_limit`；
- 网页显示链接取得、过期识别、刷新与内容验证的实际过程：事件时间线逐条展示并附证据；
- 1280×720 首屏可见关键操作与结果，证据可展开：`webapp/static/app.css` 双栏布局。
