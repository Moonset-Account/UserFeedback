# 研究讲座音频目录采集器

仅采集**订阅元数据**与 **enclosure（音频附件）可用性**的本地研究工具。
本项目**不包含播放器**、不下载完整音频、不依赖真实账户或任何外部业务服务，
所有样例数据（机构、讲座、人物、音频、封面）均为虚构并由项目自行生成。

## 1. 环境要求

- Python 3.11+（开发与验证版本：3.11.6，macOS）
- 现代浏览器（仅端到端测试需要 Chromium，由 Playwright 自动管理）
- 无需 Docker、无需外网账户

## 2. 安装与启动（统一入口，自动使用项目环境）

```bash
# 1. 安装运行时依赖（仅 lxml 一个第三方库）
python3 -m pip install -r requirements.txt

# 2. 统一启动方式（首次运行会自动创建 .venv 并安装依赖，无需手动激活）
python3 app.py --host 127.0.0.1 --port 8765
```

启动后打开 <http://127.0.0.1:8765/> 即可看到中文操作页面。
`app.py` 的引导逻辑：若项目下没有 `.venv` 会自动创建虚拟环境、安装
`requirements.txt`，再用该环境重新执行自身；已存在则直接复用，无需另外激活环境。

页面操作：

- **“运行示例（正常 + 边界）”**（id=`run-demo`）：用小样例依次执行一个正常场景
  与一个边界场景，本地实测两场景合计约 0.5 秒（验收要求 15 秒内），两个结果都保留可切换。
- **“运行完整验收场景”**：覆盖更正版本、媒体换址、超时、非法 XML、未知协议、
  404 源与订阅源身份冲突（含 2 秒超时源，整体约 2–4 秒）。
- **自定义输入**：每行一个来源（`URL 空格 版本标签`），支持 `http(s)://` 与
  本地文件路径 / `file://` URI，可配置超时与是否探测可用性。

## 3. 测试命令

```bash
# 全部单元测试 + HTTP 端到端 + 架构守卫（54 项，无需浏览器，约 20 秒）
python3 -I -m unittest discover -t . -s tests

# 仅浏览器端到端（Playwright，4 项；需先安装浏览器）
python3 -m pip install -r requirements-dev.txt
python3 -m playwright install chromium
python3 -I -m unittest discover -t . -s tests -p "test_playwright_e2e.py"
```

`-I`（隔离模式）保证测试不受当前目录中同名模块等环境污染。
测试布局：

| 测试文件 | 内容 |
| --- | --- |
| `tests/test_parser.py` | xml:base 多级链、guid permalink 差异、Atom id、多链接关系、P5 指纹、非法文档拒绝 |
| `tests/test_identity.py` | 同名不同实体、媒体换址身份不变、重复身份不覆盖、标识重叠、更正版本修订链 |
| `tests/test_architecture_guard.py` | 采集核心禁止依赖样例生成器/夹具目录/测试真值；webapp 仅允许惰性导入样例模块 |
| `tests/test_end_to_end.py` | 启动本地源站经 HTTP 真实采集，对照独立真值断言全部验收点（含 404/410/503、HEAD→GET 回退、超时） |
| `tests/test_playwright_e2e.py` | 浏览器自动化：`run-demo` 15 秒时限、三栏渲染、冲突与错误显式展示、任务结果保留切换 |

## 4. 支持的协议子集（明确声明）

**RSS 2.0**
- 必需/常用元素：`channel/title/link/description`；`item/title/link/guid/pubDate/author/description`
- 附件：`enclosure@url|type|length`
- 扩展：`itunes:image`（订阅源级与条目级封面）、`media:content`、`media:thumbnail`、
  `content:encoded`、`dc:creator`，以及 RSS 文档内的命名空间 `atom:link`
- `guid` 的 `isPermaLink` 三态（`true` / `false` / 缺省）分别处理并记录解释

**Atom 1.0（RFC 4287 子集）**
- `feed/title/id/updated/logo/icon/link`；`entry/title/id/updated/published/summary/author/link`
- `link@rel`：`alternate`、`self`、`enclosure`、`related`、`via`、`payment`、
  `edit`、`icon`、`logo`；缺失 `rel` 标记为 `unknown`，不臆测用途
- 多 enclosure（同一讲座多种音频格式）

**XML Base（W3C xml:base）**
- 支持根元素、条目元素、链接/附件元素上的嵌套声明，逐级 `urljoin` 解析相对地址，
- 每一步 base 都保留在 `base_chain` 证据中；无声明时以订阅源抓取地址兜底。

**传输与输入**
- `http://` / `https://`（标准库 `urllib`，无鉴权、无自定义请求体）
- 本地文件路径与 `file://` URI
- 可用性探测：先 `HEAD`，遇 405/501 自动退回 `Range: bytes=0-0` 的 `GET`；
  记录状态码、Content-Type、Content-Length、重定向链、耗时与具体错误。

**明确不支持**（遇到即报错并在页面展示，不猜测）：RSS 1.0/RDF、JSON Feed、
HTML 抓取、XSLT/实体扩展（解析器关闭 DTD/网络/实体）、OAuth 等鉴权订阅、
播客平台私有 API。

## 5. 身份优先级（可解释，禁止按标题或媒体 URL 唯一化）

条目身份按以下优先级取最高可用线索，每条线索都保留元素片段与中文解释：

| 优先级 | 线索 | 说明 |
| --- | --- | --- |
| P1 | Atom `<id>` | 规范定义的永久唯一标识 |
| P2 | RSS `<guid>` | 区分 `isPermaLink=true/false/缺省`；不透明 guid 不当 URL 打开 |
| P3 | 订阅源作用域内 permalink | 仅在同一 feed 身份作用域内有效，不能跨源唯一化 |
| P4 | 首个 `alternate` 链接 | 与 P3 同源作用域限制 |
| P5 | 结构指纹（兜底弱标识） | 标题+日期+作者+链接+附件地址的 SHA-256，仅同 feed 内参与归并 |

订阅源自身身份（F-P1 Atom feed id → F-P2 `rel=self` → F-P3 站点链接 → F-P4 抓取地址）。

冲突处理原则——**任何冲突都显式展示、保留全部条目与 XML 证据，绝不静默覆盖**：

1. 同一快照重复身份键（两个条目声明同一 guid / atom:id）→ `duplicate_identity` 冲突，拆成两个目录条目；
2. 一个条目的身份线索与另一条目重叠（如 C 的 guid permalink 等于 D 的 link）→ `identifier_overlap`；
3. 标题相同但身份不同 → `duplicate_title` 警告，各自独立建目；
4. 不同 feed 身份声明了相同 `self` 链接 → `cross_feed_identifier`；
5. 同一身份跨快照出现 → 归并为一个实体的 **revision 修订链**；附件地址变化记为
   **媒体更换事件**（身份不变，新旧 URL 与新旧 XML 片段同时保留）。

## 6. 目录结构与模块边界

```
app.py                     统一启动入口（自动 venv 引导）
feedcollector/             采集器（核心模块禁止 import samplegen，有架构守卫测试）
  parser.py                RSS/Atom/xml:base 解析与身份线索提取
  identity.py              优先级归并、冲突检测、修订链与媒体更换
  fetcher.py               HTTP/文件抓取、HEAD/Range-GET 可用性探测
  collector.py             采集编排与报告
  storage.py               任务结果 JSON 存储
  webapp.py                中文页面 + JSON API（编排层，惰性导入样例模块）
  models.py / errors.py    数据模型与异常
samplegen/                 样例生成器与本地源站（与采集器零核心耦合）
  scenarios.py             虚构 RSS/Atom 夹具与仅供测试的 expected 真值
  source_server.py         本地 HTTP 源站（含 404/410/503/405/超时端点）
  mediafactory.py          最小合法 WAV/MP3/PNG 生成
static/                    原生 HTML/CSS/JavaScript 中文页面（无构建步骤）
tests/                     独立断言、HTTP 端到端、Playwright E2E
data/                      运行时生成：fixtures/ 夹具、runs/ 任务结果（不入库）
```

边界规则：

- 采集器只接受 **URL / 文件路径**输入，通过规定的公开 HTTP 接口或文件读取样例；
- `samplegen/scenarios.py` 中的 `expected_*` 是**独立真值，仅 tests 使用**；
  提供给页面与采集器的场景清单由 webapp 剥离真值字段后下发；
- `feedcollector` 核心包不出现任何夹具路径或真值字段引用（CI 守卫强制）。

## 7. 验收场景对照

| 验收要求 | 覆盖位置 |
| --- | --- |
| 媒体地址更换但条目不变 | `full-acceptance`：stable-guid-001 两版本，媒体更换事件 + 2 个 revision |
| 重复标题 | 两个“量子纠错入门”（不同 guid）+ 边界源“同名讲座：边缘计算” |
| 相对 enclosure | 根/条目/元素三级 xml:base 解析（battery、deep-base、rel-demo） |
| 媒体不可用 | 404 / 410 / 503 端点，状态码与错误原因逐条展示 |
| 标识冲突 | 重复 guid、重复 atom:id、permalink/link 重叠，双方证据并列不覆盖 |
| 更正版本 | correction-guid-009 初稿/勘误归并为同一实体两个 revision |
| 并排三栏 | 订阅源原始 XML 片段 ｜ 采集条目与可用性 ｜ 各链接 rel 用途与 base 链 |
| 具体错误可见 | 抓取/解析/探测三阶段错误，含来源 URL、状态码、阶段与异常类型 |

## 8. 已知边界（如实说明）

- 只做目录采集与可用性探测，**没有播放器、没有音频转码或下载管理**；
- 可用性是探测时刻的快照结论，不做长期可用性监控或告警；
- 日期保留原始文本并展示，不做跨时区归一化（协议子集外能力）；
- 认证订阅、JSON Feed、RSS 1.0 不在支持子集内，遇到会给出明确错误而非猜测解析。
