# 数字标本 IIIF Presentation 3 清单采集器

从 **IIIF Presentation API 3.0** 清单中采集数字标本的图像资源描述与标本页面顺序，
区分 Canvas 标识、图像标识与章节（Range）引用，支持多绘制 Annotation、Choice 资源、
层级 Range、循环 Range 检测，并通过中文网页展示采集结果、来源证据与具体错误路径。

全部数据为**虚构样例**，不依赖真实账户、外部业务服务或 Docker。

---

## 1. 快速开始（统一入口，无需手动激活虚拟环境）

要求 Python 3.11+，可访问 PyPI（仅首次启动需要网络，用于安装依赖）。

```bash
python3 app.py --host 127.0.0.1 --port 5077
```

首次启动会自动在项目内创建 `.venv` 并安装 `requirements.txt`，随后用虚拟环境 Python
重新执行本入口。之后打开浏览器访问：

```
http://127.0.0.1:5077/
```

- 点击右上角 **「运行示例」**（`id=run-demo`）：用小样例依次运行一个正常场景与一个边界场景，
  实测接口耗时约 0.02 秒，远低于 15 秒目标，两次结果均保留可切换查看。
- 「运行完整验收场景」按钮：运行 6 Canvas、含 Choice / 共享图像 / 自循环 Range 的完整场景。
- 也可在输入框填写任意清单 URL（http/https）或**本地文件路径**后点击「运行采集」。
- 页面按 1280×720 适配，关键操作与统计在首屏可见，问题证据等可展开查看。

---

## 2. 依赖安装与测试命令

入口已自动管理虚拟环境。如需手动安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Playwright 浏览器（仅端到端测试需要）
.venv/bin/python -m playwright install chromium
```

运行测试（测试会在随机端口自动启动本地源站，结束后关闭）：

```bash
# 全部测试：16 个（单元 + 集成 + Playwright 端到端）
.venv/bin/python -m pytest -q

# 仅核心逻辑与源站/隔离测试（无需浏览器）
.venv/bin/python -m pytest tests/test_collector.py tests/test_sample_site.py tests/test_isolation.py -q

# 仅浏览器端到端
.venv/bin/python -m pytest tests/test_e2e_playwright.py -q
```

独立生成样例源站（可选，Web 启动时也会自动生成到 `data/sample/`）：

```bash
.venv/bin/python -m sample_site.generator --out data/sample --base-url http://127.0.0.1:5077/sample
```

---

## 3. 支持的 IIIF Presentation 3 协议子集

采集器针对以下结构实现，超出子集的内容会记录带 JSON 路径的 Issue 并跳过，不影响其余页面：

| 结构 | 支持情况 |
| --- | --- |
| 顶层 `Manifest`（`@context`/`id`/`type`/`label`/`items`/`structures`） | 支持；非标准 `@context`/`type` 给警告但仍尝试解析 |
| `Canvas`（`id`/`type`/`label`/`width`/`height`/`items`） | 支持，保留清单内页面顺序 |
| `AnnotationPage` → `Annotation` | 支持 |
| `painting` 标注，`body` 为 `Image` | 支持；非 painting 标注（如 commenting）跳过并警告 |
| `Choice` 资源（`type=Choice`，`items` 多候选） | 采集**全部候选**，首个为默认、其余标记 `is_choice_alternative` |
| 图像 `service`（ImageService3） | 仅记录 `id`/`type`/`profile`，**不做动态图像编辑/解引用** |
| `Range`（`structures`） | 支持 Canvas / SpecificResource（source、target）/ Range 三类引用与嵌套 |
| 循环 Range | DFS 检测回边，标记 `cyclic` 并跳过回边，保证解析不死循环 |
| 悬空引用、重复 ID、缺字段、非 JSON 等 | 记录 error/warning 级 Issue，含 `/items/i/...`、`/structures/i/...` 具体路径 |

**明确不做**：图像编辑（裁剪/缩放/旋转等 IIIF Image 参数服务）、通用相册功能、
Presentation 2 兼容、Collection 清单聚合、外部鉴权内容。

本地仅提供 **IIIF Image API 3.0 level 0 静态样例**（`info.json` + 静态全图/缩略图），
不实现动态图像处理端点。

### 三类标识严格区分

- **Canvas 标识**：页面身份（`/canvas/...`）；
- **图像标识**：图像资源身份（body 的 `id`，`/image-service/.../default.jpg`）；
- **章节引用**：Range `items` 中对 Canvas / Range 的引用。

同一图像 ID 被多个 Canvas 使用时，每个 Canvas 各自保留该图像，**绝不合并页面**，
仅产生可追溯的 `SHARED_IMAGE_ID` 提示；章节复用 Canvas 时同样各自保留引用关系。

---

## 4. 目录结构与采集边界

```
app.py                     统一启动入口（自动建 venv、装依赖、起服务）
iiif_collector/            采集器（只依赖 requests + 标准库）
  fetcher.py               抓取层：仅经公开 HTTP(S) 或本地文件读取
  parser.py                Presentation 3 子集解析、Choice、Range 循环检测
  models.py                采集结果数据模型与 Issue
  collector.py             对外 collect(source) 入口
sample_site/
  generator.py             样例生成器（虚构数据；纯标准库生成 PNG/清单/Image 服务样例）
webapp/
  app.py                   Flask 路由：源站托管、采集 API、运行记录
  run_store.py             运行记录（内存 + data/runs/*.json 落盘）
  static/                  中文网页（index.html / app.css / app.js）
tests/
  expected_truth.py        独立预期真值（仅测试使用，采集器与 Web 均不 import）
  test_collector.py        核心逻辑断言（对照真值）
  test_sample_site.py      源站静态资源与 Image 服务样例
  test_isolation.py        架构隔离测试
  test_e2e_playwright.py   浏览器端到端（1280×720、run-demo、15 秒目标等）
data/                      运行期生成（样例源站、运行记录），不入库
```

**采集器与样例生成器分离**：

- `iiif_collector/` 不 import `sample_site/` 或 `tests/expected_truth.py`（有隔离测试守护）；
- 采集只经过规定的公开接口（HTTP GET 清单/图像）或本地文件路径；
- 真值仅供 pytest 断言使用，采集过程不读取真值。

---

## 5. 验收场景对照

| 验收要求 | 覆盖位置 |
| --- | --- |
| 章节复用 Canvas | 正常场景 r1、r2 均引用 c2；完整验收附录复用地质篇页面 |
| 一个 Canvas 多个图像 | c1 含 3 个 painting 标注；完整验收 f1 含 3 张 |
| Choice 资源 | 正常场景 c2、完整验收 f2，候选全部采集并标记 |
| 缺失图像描述 | 边界场景 `moss-201-scan`、完整验收 `shell-118-ruler`（MISSING_IMAGE_LABEL，带路径） |
| 循环 Range | 边界场景 loop-a↔loop-b；完整验收 loop-x 自循环（CYCLIC_RANGE_REF，回边跳过） |
| 悬空引用 | 边界场景 ghost Canvas / ghost Range（DANGLING_*，带路径与 ref） |
| 无法解析部分给具体路径、其余页面可核验 | 状态 `partial`，问题表含 JSON 路径，可用 Canvas 全部保留 |
| 同一图像多处不合并页面 | SHARED_IMAGE_ID 提示 + 页面数不变（测试断言） |
| 不实现图像编辑/通用相册 | 仅 level0 静态样例与只读采集页面 |

---

## 6. 已知边界与说明

- 清单整体无法获取（网络错误、404、非法 JSON）时任务状态为 `failed`，仍会保留运行记录并展示原因；
- 图像文件本身不下载入库，网页通过源站 URL 展示缩略图；
- 全尺寸样例图以 PNG 编码承载 `.jpg` 静态路径（样例占位，浏览器按内容正常渲染）；
- Web 服务使用 Flask 开发服务器，定位为本地操作入口，未做生产级部署加固。
