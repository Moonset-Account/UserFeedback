# 实验仪器 SOAP/MTOM 响应采集台

一个从零可实际运行的本地项目：模拟虚构实验仪器返回 **SOAP Envelope + 命名空间化字段 + SOAP Fault + 经 `xop:Include` 引用的 MIME 附件（MTOM 风格）**，由独立采集器按 **Content-ID** 关联测量数据，完成安全解析、附件解码与摘要校验，并在中文网页上展示正式结果、可解释错误与逐字节来源证据。

> 全部数据均为项目内确定性虚构生成，不依赖任何真实账户、外部业务服务或 Docker。

---

## 1. 支持的协议子集（LabInstr SOAP/MTOM v1，虚构协议）

- **SOAP 1.1 / 1.2 Envelope**（本项目样例使用 SOAP 1.1，解析器同时兼容 1.2 的 Fault 结构）；
- **`multipart/related`**（RFC 2387 风格）：外层带 `type` / `boundary` / `start` 参数，根 part 按 `start` 定位，缺省取首个 part；
- **`xop:Include`**：以 `href="cid:..."`（兼容带/不带尖括号）引用 MIME 附件；
- **Content-ID 关联**：关联键只有 Content-ID，**与附件在报文中的出现顺序无关**；
- **SOAP Fault**：业务错误结构化返回（Fault 场景按协议使用 HTTP 500）；
- **LABSMP v1**：虚构二进制测量样本格式 `application/x-fictional-lab-sample`；
- 解析基于成熟开源库：**defusedxml**（安全 XML）、标准库 `email`（MIME）、**requests**（HTTP）、**Flask**（页面）。

### 明确不支持 / 已禁用

- **禁用一切 DOCTYPE、内部/外部实体、外部 DTD、XInclude 网络加载**（字节层硬拦截 + `defusedxml` 的 `forbid_dtd/forbid_entities/forbid_external` 双保险），因此 XXE 与网络实体解析无法触发；
- 不支持非 `multipart/related` 的外层类型；不支持按附件出现顺序“猜测”关联；重复 Content-ID 直接判冲突。

---

## 2. 目录结构

```
.
├── app.py                     # 统一启动入口（自动 venv 引导 + 样例生成 + 源站 + 网页）
├── requirements.txt
├── labsoap/                   # 采集器（核心逻辑，不 import 生成器/测试真值）
│   ├── collector.py           # 主编排：取数→MIME→安全XML→CID关联→解码→摘要校验
│   ├── mime_parser.py         # multipart/related 解析、CID 索引、重复/根 part 规则
│   ├── envelope.py            # SOAP Envelope/Fault/命名空间字段/xop:Include 提取
│   ├── secure_xml.py          # 禁用 DTD/外部实体/网络实体的安全 XML 解析
│   ├── sample_format.py       # LABSMP v1 二进制样本解析
│   ├── digest.py              # 通道摘要（覆盖 XML 字段 + 附件原始字节）
│   ├── sources.py             # 规定的两类入口：本地 HTTP 接口 / samples 白名单文件
│   └── errors.py              # 错误码、状态、协议子集说明
├── generator/                 # 样例生成器（与采集器分离，仅经公开接口被采集）
│   ├── scenarios.py           # 六个虚构场景的确定性数据集
│   ├── mime_builder.py        # 手工构建 SOAP/MTOM 报文
│   ├── sample_writer.py       # 落盘 samples/*.mime
│   └── server.py              # 虚构仪器本地源站（标准库 http.server）
├── web/                       # Flask 中文页面（templates / static）
├── samples/                   # 启动时自动生成的六个 .mime 样例（git 忽略）
└── tests/
    ├── expected/expected_values.py   # 独立预期真值（独立字面量，不 import 生成器）
    ├── test_unit_parsing.py          # 单元：MIME/LABSMP/XXE 防护
    ├── test_integration.py           # 集成：HTTP 与文件两条入口对照真值
    ├── test_separation.py            # 架构隔离：采集器不得读取真值/生成器
    ├── test_web_api.py               # Web API 真实执行与 15 秒预算
    └── e2e/test_browser.py           # Playwright 浏览器端到端（未装则跳过）
```

---

## 3. 环境要求与安装

- Python **3.11+**；
- 依赖见 `requirements.txt`：`flask`、`requests`、`defusedxml`、`pytest`，可选 `playwright`。

```bash
python3 -m pip install -r requirements.txt
# 仅当需要浏览器端到端测试时：
python3 -m playwright install chromium
```

---

## 4. 启动方式（统一入口，无需手动激活虚拟环境）

```bash
python3 app.py --host 127.0.0.1 --port 8090
```

启动器会：

1. 若未在项目 `.venv` 内运行，自动创建虚拟环境（`--system-site-packages`，离线可用）并按需安装依赖，然后用该解释器**重新执行自身**——无需手动 `source .venv/bin/activate`；
2. 生成六个虚构样例到 `samples/`；
3. 在 `端口+1`（如 8091）启动虚构仪器源站；
4. 在指定端口（如 8090）启动中文采集台。

浏览器打开 **http://127.0.0.1:8090/** 即可操作。

### 页面能做什么

- 选择输入来源：**内置服务 HTTP 接口** 或 **项目生成的样例文件**；
- 选择单个场景运行；
- **`运行示例`（id=`run-demo`）**：用小样例依次跑一个正常（complete）与一个边界（duplicate_cid）场景，本地实测约十几毫秒，远在 15 秒内结束，并保留两张结果卡片；
- **`完整验收场景`**：一次跑完全部 6 个场景；
- 每个结果卡片可展开：测量表格（XML 字段来源 + 附件字节来源 + 摘要核对）、可解释错误、part 清单（Content-ID/类型/字节数/SHA-256/Hex 预览）与原始 MIME 的 base64。

页面针对 **1280×720** 设计，关键操作与结果在首屏可见，详细证据按需展开。所有结果均来自真实执行，页面不预填任何成功数据。

---

## 5. 六个虚构场景与验收口径

| 场景 | 构造 | 采集结论 | 错误码 |
| --- | --- | --- | --- |
| `complete` | 2 个测量组、4 个通道，摘要全部正确 | `complete`（进入正式结果） | — |
| `fault` | 仪器校准过期的业务 SOAP Fault（HTTP 500） | `fault` | `SOAP_FAULT` |
| `missing_reference` | 一个 `xop:Include` 引用的 Content-ID 无附件 | `rejected` | `MISSING_REFERENCE` |
| `duplicate_cid` | 两个附件 part 使用同一 Content-ID | `rejected` | `DUPLICATE_CONTENT_ID` |
| `parts_reordered` | 附件全部先于 XML 根 part 到达 | `complete`（验证不依赖顺序） | — |
| `digest_mismatch` | 篡改 XML 摘要，使重算不一致 | `rejected` | `SUMMARY_DIGEST_MISMATCH` |

验收要点全部覆盖：**一个完整结果、一个业务 Fault、一个缺失引用、一个重复标识冲突**；只有完整且每个通道摘要正确的测量组进入正式结果，其余仅保留原始响应与可解释错误供展开核对。

### 关联与来源规则

- 先建立全量 Content-ID 索引：**重复即报 `DUPLICATE_CONTENT_ID`**（记录冲突 part 序号，绝不静默取第一个）；
- `xop:Include` 只按 cid 查索引，附件先到/后到不影响结果；引用缺失报 `MISSING_REFERENCE`，未被引用的孤立附件报 `DANGLING_ATTACHMENT`；
- 每个解析数值同时保留两类证据：XML 字段（`lab:Code`/`lab:Unit`/`lab:SummaryDigest`）与附件字节（part 序号、字节数、SHA-256、Hex/Base64）；
- 附件内嵌测量标识还须与 XML 的 `MeasurementId` 一致，否则 `MEASUREMENT_ID_MISMATCH`；
- 摘要算法：`sha256( utf8(channel_code) + NUL + 附件原始字节 )`，与 XML 中 `SummaryDigest` 逐字符比较。

---

## 6. 测试命令

```bash
# 全部测试（已安装 playwright+chromium 时含浏览器 E2E，否则 E2E 自动 skip）
python3 -m pytest -q

# 仅核心逻辑与接口测试
python3 -m pytest tests/test_unit_parsing.py tests/test_integration.py \
                 tests/test_separation.py tests/test_web_api.py -q

# 仅浏览器端到端
python3 -m pytest tests/e2e -q
```

`tests/expected/expected_values.py` 是**独立维护的预期真值**（纯字面量，不导入生成器或采集器）；`test_separation.py` 用 AST 断言采集器包既不导入 `generator`，也不读取测试真值——采集只能经规定的 HTTP 公开接口或 samples 文件进行，真值仅供测试比对，不能代替采集。

---

## 7. 本地源站公开接口（采集器唯一网络入口）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/scenarios` | 场景目录 |
| POST | `/soap?scenario=<name>` | 返回该场景完整 `multipart/related` 响应（fault 场景为 HTTP 500） |

源站也可单独运行：`python3 -m generator.server --host 127.0.0.1 --port 8091`。

---

## 8. 已知边界与如实说明

- 报文为项目内手工构建的确定性虚构样本，未实现 MTOM 的 HTTP 分块传输、MTOM 响应回调等真实传输特性；协议子集以第 1 节为准。
- LABSMP v1 是为本项目定义的虚构二进制格式，仅含采样点与内嵌测量标识。
- 浏览器 E2E 需要本机具备 Playwright 的 Chromium；未安装时该测试自动跳过，其余 28 项核心/接口测试不受影响。
