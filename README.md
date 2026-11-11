# WARC 研究资料字段提取工具

从本地 WARC 网页归档中提取研究资料字段（标题、作者、发布时间、摘要、关键词、正文字数），
以「字段 ↔ 归档来源」卡片呈现，支持跳转到选中记录的**原始响应**取证。
全部数据为虚构内容，仅在本机 127.0.0.1 运行，不依赖真实账户、外部业务服务或 Docker。

## 功能要点

- 按记录长度（`Content-Length`）分帧读取 `response`、`request`、`revisit`，
  并严格区分 **WARC-Record-ID（归档记录标识）** 与 **WARC-Target-URI（目标 URL）**；
- **revisit 仅按明确引用还原有效正文**，绝不把「最近一个同 URL 正文」当作兜底目标：
  - `WARC-Refers-To`（记录 ID 精确引用，最高优先级）；
  - `WARC-Refers-To-Target-URI` + `WARC-Refers-To-Date`（URL+日期精确匹配，日期做 UTC 归一化）；
  - 引用不存在、引用类型不可复用、被引用记录坏摘要、摘要交叉核对失败 → 结构化报错，不复用；
- revisit 复用旧正文时，**HTTP 响应头仍取本 revisit 记录**（支持「正文不变、响应头变化」）；
- 多成员 gzip 逐成员切分：单个成员解压失败自动重新同步到下一个成员签名，
  坏成员被隔离，**其后的正常记录仍可读取**；
- 坏摘要（`WARC-Payload-Digest` 与实际正文不符）显式标记，字段仍提取供人工研判；
- 同 URL 多个时间版本逐条独立提取、独立断言；
- 不提供任何归档文件管理功能（无删除、列表、重命名、下载接口）。

## 支持的协议子集

| 维度 | 支持范围 |
| --- | --- |
| 容器格式 | WARC 1.0 / 1.1；未压缩 `.warc` 与多成员 gzip `.warc.gz` |
| 记录类型 | `warcinfo`、`response`、`request`、`revisit`、`resource`（其他类型透传计数） |
| revisit profile | WARC 1.1 `server-not-modified`、WARC 1.0 `identical-payload-digest` |
| 引用解析 | `WARC-Refers-To`；`WARC-Refers-To-Target-URI` + `WARC-Refers-To-Date` |
| 关联 | `WARC-Concurrent-To`（request ↔ response） |
| HTTP 解帧 | 状态行/头部解析、`Content-Length`、`Transfer-Encoding: chunked` |
| Content-Encoding | `identity`、`gzip`/`x-gzip`、`deflate`（其余原样保留并告警） |
| 摘要 | `sha1`、`sha256`；base32（RFC4648 无填充，WARC 惯例）与十六进制 |
| HTML 字段 | 标准库 `html.parser`，`title`、`meta`、`h1`、`time`、`p`、`a` |

**不支持/未覆盖**：ARC 旧格式、WARC 分段（continuation/segment）、`br`/`zstd` 等
Content-Encoding、JavaScript 渲染页面（字段只来自静态 HTML）、大于 50MB 的上传。

协议解析使用成熟开源库 [warcio 1.7.4](https://github.com/webrecorder/warcio)
（HTTP 头部解析、chunked 读取、WARC 记录读写）；gzip 成员切分与长度分帧为本项目实现。

## 目录结构

```
attempt-001/
├── app.py                     # 统一入口（自动引导 .venv）
├── requirements.txt
├── warc_tool/
│   ├── reader.py              # gzip 成员切分 + 长度分帧 + HTTP 解帧
│   ├── digests.py             # 摘要计算/校验（base32/hex）
│   ├── extract_fields.py      # HTML 字段提取（含证据定位器）
│   ├── pipeline.py            # 提取流水线 + revisit 显式引用还原
│   ├── warc_sink.py           # WARC 写入（支持故意构造坏摘要）
│   ├── samplegen.py           # 样例归档生成器（与采集器分离）
│   ├── source_content.py      # 虚构源站页面内容（采集器不导入）
│   ├── source_server.py       # 虚构源站 Flask 服务
│   ├── harvester.py           # 采集器（仅走公开 HTTP 接口）
│   ├── runner.py              # Web 任务管理（隔离子进程）+ 取证接口
│   ├── web_app.py             # Web 后端 API
│   └── cli_extract.py         # 命令行提取入口
├── web/                       # 中文前端（原生 HTML/CSS/JS，无构建）
├── tests/                     # 52 个测试，含独立真值与 Playwright E2E
│   └── expected_truth.py      # 独立预期真值（不导入生成器/源站）
├── sample_outputs/            # 生成的样例 WARC
└── uploads/                   # 上传归档的隔离处理目录
```

## 安装与启动

要求 Python 3.11+。无需手动创建/激活虚拟环境——`app.py` 首次运行时若发现
项目内没有 `.venv`，会自动创建并安装 `requirements.txt`，随后用该环境重新执行自身。

```bash
# 1. 启动 Web 应用（统一启动方式）
python3 app.py --host 127.0.0.1 --port 8080
# 浏览器打开 http://127.0.0.1:8080/ 即可操作
```

其他子命令：

```bash
python3 app.py source --host 127.0.0.1 --port 8123   # 启动虚构源站
python3 app.py gen-samples                           # 生成三份样例 WARC
python3 app.py extract sample_outputs/demo-normal.warc.gz --pretty  # 命令行提取
```

## 网页操作

页面针对 1280×720 设计，关键操作与结果在首屏可见，详细证据在抽屉/折叠区展开：

1. **配置输入**：选择本地 `.warc` / `.warc.gz`（≤50MB）上传，或使用内置样例；
2. 点击 **`运行示例`（id=`run-demo`）**：用小样例依次运行一个正常场景与一个边界场景，
   实测两个任务合计 **1 秒内**完成（验收上限 15 秒），两个结果都保留在任务队列；
3. 「完整验收场景」按钮运行覆盖全部规则的大样例；
4. 结果区为字段卡片：记录类型、目标 URL、WARC-Record-ID、摘要状态、字段值与证据定位器、
   revisit 复用来源、错误/警告；
5. 点击卡片上的「查看原始响应 / 来源证据」打开抽屉，查看 WARC 头部、HTTP 响应行/头部、
   原始负载与 gzip 成员/文件偏移。

## 样例场景（全部虚构）

| 样例 | 覆盖内容 |
| --- | --- |
| `demo-normal.warc.gz` | 同 URL 两版正文；revisit **复用旧正文但响应头变化**（ETag/Last-Modified 不同） |
| `demo-boundary.warc.gz` | 重复 URL；revisit 显式引用旧版（最近同 URL 是新版的陷阱）；**缺失引用**；**坏摘要**；**损坏 gzip 成员**且其后仍有正常记录 |
| `acceptance.warc.gz` | 上述全部 + request/`WARC-Concurrent-To` 关联 + WARC 1.0 profile（URI+Date 引用）+ 非 HTML 负载 |

## 测试命令

```bash
# 安装依赖（若尚未自动安装）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 全部后端 + 集成测试（50 个，约 2 秒）
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_e2e_browser.py

# Playwright 端到端（首次需安装浏览器，本机已缓存可跳过）
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/test_e2e_browser.py -q

# 完整套件（52 个）
.venv/bin/python -m pytest tests/ -q
```

测试分层：

- `test_digests.py`：摘要算法/编码/坏标签；
- `test_reader_robustness.py`：未压缩 WARC、垃圾前缀重同步、多成员、截断、chunked、gzip 编码；
- `test_extract_fields.py`：字段与证据、相对链接、字符集回退、script/style 剔除；
- `test_pipeline_acceptance.py`：**核心验收**，各版本独立断言、禁止「最近同 URL」兜底、
  缺失引用、坏摘要、坏成员隔离；
- `test_harvester_integration.py`：对真实源站进程走公开 HTTP 接口采集（含结构约束：
  采集器源码不得 import 源站内容模块）；
- `test_web_api.py`：上传/任务/取证 API 与 15 秒时限；
- `test_e2e_browser.py`：Playwright 真实浏览器操作中文页面。

`tests/expected_truth.py` 是**独立维护的预期真值**，刻意不导入 `samplegen`/`source_content`，
不能用读真值代替采集；采集器只能通过源站公开 HTTP 接口（含 `X-Sim-Date` 版本选择头）获取页面。

## 安全说明

上传归档按不可信数据处理：提取在 `python -I`（隔离模式，忽略环境变量与用户 site-packages）
子进程中执行，仅传入绝对路径，不切换工作目录，避免同目录恶意模块注入。
