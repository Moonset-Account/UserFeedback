# 实验通知只读采集器 + 本地最小邮箱协议模拟服务

一个完全离线、可实际运行的演示项目：用本地 **最小 IMAP4rev1 模拟服务**扮演邮箱，
采集器以**只读**方式经协议拉取实验通知邮件，解析 MIME 结构并提取**实验批号**与
**附件中的数值**，全部数据为虚构内容，**不连接真实邮箱、不发送任何邮件**。

## 1. 环境要求

- Python 3.11+（开发与验证版本：3.11.6）
- 操作系统自带的 `venv` 与 `pip`（能访问 PyPI 安装依赖即可）
- 不需要 Docker、不需要任何真实邮箱账户或外部业务服务
- 浏览器自动化使用 [Playwright](https://playwright.chromium.org/)（仅测试用，可选）

## 2. 一键启动（无需手动激活虚拟环境）

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

首次启动时 `app.py` 会自动在项目目录创建 `.venv/` 并安装 `requirements.txt`，
随后用虚拟环境解释器重新执行自身。之后打开浏览器访问：

```
http://127.0.0.1:8000/
```

页面为中文操作入口，针对 1280×720 适配：首屏即可看到输入配置、三个运行按钮与
结果总览表，来源证据与错误在下方可展开查看。

- **运行示例**（按钮 id：`run-demo`）：小样例依次运行一个正常场景
  （`normal_basic`）和一个边界场景（`bad_attachment`），实测约 1 秒完成，
  目标 15 秒内，两次结果均保留可查看。
- **运行所选场景 / 运行全部验收场景（5 个）**：更完整的验收入口。

## 3. 安装与测试命令

```bash
# 运行时依赖（app.py 首次启动也会自动执行）
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 开发/测试依赖（含 pytest、playwright）
.venv/bin/python -m pip install -r requirements-dev.txt

# 仅生成全部虚构样例（Web 首次启动也会自动生成，幂等）
.venv/bin/python -m mailacq.generator.generate

# 运行全部测试（31 个）
.venv/bin/python -m pytest tests/ -q

# 浏览器 E2E 需要先安装 Chromium（一次性）
.venv/bin/python -m playwright install chromium
```

未安装 Playwright 或 Chromium 时，浏览器 E2E 会自动 skip，不影响其余测试。

## 4. 支持的协议子集（明确声明）

模拟服务 `mailacq/collector/mock_imap.py` 是纯标准库实现的最小 IMAP 服务器，
采集器 `mailacq/collector/imap_client.py` 使用标准库 `imaplib` 对接。
双方只使用以下 **IMAP4rev1 只读子集**（RFC 9051 的极小切片）：

| 阶段 | 支持的命令 / 特性 |
|---|---|
| 连接 | TCP 行协议、服务器问候、请求/响应中的 `{n}` literal |
| 未认证 | `CAPABILITY`、`LOGIN`（任意虚构凭据）、`LOGOUT` |
| 已认证 | `NOOP`、`SELECT`/`EXAMINE`（始终 READ-ONLY）、`STATUS (UIDVALIDITY UIDNEXT MESSAGES)` |
| 取信 | `UID SEARCH ALL`、`UID FETCH <uid-set> (UID FLAGS RFC822)` |
| 拒绝 | `STORE` / `APPEND` / `DELETE` / `EXPUNGE` / `COPY` / `MOVE` / `CREATE` / `RENAME` 等写命令一律返回 `NO` |

MIME 解析使用 Python 标准库 `email`（default policy，覆盖 RFC 5322、RFC 2045/2047、
RFC 2231、RFC 2046 multipart）。**不支持也不实现** SMTP、POP3、IDLE、ACL、
QUOTA、TLS/STARTTLS、AUTHENTICATE 等；不做任何外发连接。

## 5. 五类验收场景（全部虚构）

| key | 验收情形 | 要点 |
|---|---|---|
| `normal_basic` | 正常基线 | 嵌套 `multipart/mixed` → `multipart/alternative`；Base64 与 Q 两种编码主题；RFC 2047 编码的中文/法文附件文件名；CSV 数值（负数、小数） |
| `dup_message_id` | **同一 Message-ID 对应两封不同邮件** | 两封邮件 Message-ID 相同但 UID、主题、批号不同，必须按 `UIDVALIDITY/UID` 区分，不得合并 |
| `uid_reset` | **代际重置后 UID 复用** | 两代邮箱都有 UID=1 但 UIDVALIDITY 不同，识别为两封独立邮件并记录 1 次代际切换 |
| `alt_conflict` | **纯文本与 HTML 内容不一致** | 两个替代版本批号不同；选用 `text/plain`，同时保留 HTML 版本证据与冲突警告 |
| `bad_attachment` | **坏附件** | 附件声明 base64 但负载非法（`InvalidBase64LengthDefect`），记录 part 级解码错误，正文提取与其他邮件不受影响 |

## 6. 身份标识与可追溯性

- 邮件唯一身份键为 **`UIDVALIDITY/UID`**（如 `3333000002/1`），Message-ID 仅作
  辅助线索；重复 Message-ID 与 UID 复用都会在“身份标识分析”面板结构化展示。
- 每个 MIME part 分配稳定 **part_id**（`1`、`1.1`、`1.1.1`、`1.2` …）。
  批号与附件数值的每个提取结果都带 `source_part_id`，网页详情可逐层展开
  原邮件 MIME 结构树并定位到选用的具体 part。
- 替代版本（plain/html）各自的匹配结果、是否被选用均有记录；附件解码错误
  保留错误类型（如 `InvalidBase64LengthDefect`）与原始头信息。

## 7. 采集器 / 样例生成器分离（防止“读真值代替采集”）

```
mailacq/
├── generator/          # 样例生成器：只写 generated/<scenario>/
│   ├── mime_builder.py # 构造 RFC 合规 MIME（含故意损坏的坏附件）
│   ├── scenarios.py    # 五个虚构场景定义
│   └── generate.py     # 落盘 .eml + manifest.json + ground_truth.json
├── collector/          # 采集器：只认识 IMAP 协议字节，禁止出现
│   ├── mock_imap.py    #   generated/manifest/ground_truth 任何字样
│   ├── imap_client.py  #   （tests/test_collector_isolation.py 静态+运行时双重校验）
│   ├── parser.py       # MIME 结构树、编码主题/文件名、坏附件缺陷
│   ├── extractor.py    # 批号与附件数值（全部带 part_id 证据）
│   └── identity.py     # UIDVALIDITY/UID 身份模型
├── runner.py           # 编排：每代启动模拟服务 → 协议采集 → 解析 → 落盘
├── storage.py          # instance/runs/ 下的 JSON 运行记录
└── webapp/             # Flask + 原生 HTML/CSS/JS 中文界面
```

数据流严格单向：

```
generator 写文件 → runner 载入字节 → MockIMAPServer ══IMAP 协议══▶ imap_client
   → parser → extractor → identity → instance/runs/*.json → 网页
```

`ground_truth.json` 是**独立真值**，只有 `tests/` 下的断言代码可以读取；
测试还会 monkeypatch 内建 `open`，令采集链路一旦触碰真值文件立即失败。

## 8. 测试覆盖说明

- `tests/test_parser.py`：编码主题（B/Q）、嵌套结构、编码文件名、坏附件缺陷捕获
- `tests/test_extractor.py`：批号来源 part、替代版本冲突、CSV 数值、坏附件结构化错误
- `tests/test_identity.py`：身份键、代际切换、UID 复用、重复 Message-ID
- `tests/test_collector_isolation.py`：静态扫描 + 运行时拦截，证明采集不读真值
- `tests/test_e2e_scenarios.py`：真实起 IMAP 服务、经 imaplib 采集，对 5 个场景
  逐项对照独立真值，并校验所有提取字段可追溯到 part_id、协议日志只含只读命令
- `tests/test_web_api.py`：Flask API 全链路，演示接口 15 秒上限断言
- `tests/test_browser_e2e.py`：Playwright 真实浏览器点击 `#run-demo`、查看证据、
  1280 宽度无横向滚动

## 9. 目录与运行产物

```
generated/      # 虚构样例（.eml、manifest.json、ground_truth.json），可随时重新生成
instance/runs/  # 每次运行的完整 JSON 记录（含 MIME 树、提取证据、协议日志、错误）
.venv/          # app.py 自动创建的虚拟环境
```

两个产物目录均已在 `.gitignore` 中忽略，删除后重新运行即可恢复。
