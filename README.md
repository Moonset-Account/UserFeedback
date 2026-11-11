# 参数序列化采集请求构造器（只读 OpenAPI 练习项目）

一个可实际运行的本地练习项目：读取项目内**限定版 OpenAPI 3.0 文档**，
构造针对虚构公开实验接口的只读采集请求，覆盖 query/path 参数的
**数组、对象、style、explode、deepObject、保留字符编码**；
本地源站回显收到的**原始请求**与**语义解析结果**，中文网页可配置输入、
运行任务、查看来源证据与具体错误。

> 全部数据均为本地虚构生成，不依赖真实账户、外部业务服务或 Docker。

## 一、快速开始

要求 Python 3.11+。统一入口（首次运行会自动在项目内创建 `.venv` 并安装依赖，
无需手动激活虚拟环境）：

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

启动后浏览器打开 <http://127.0.0.1:8000/> 即可操作。
页面左上角 **“▶ 运行示例（正常 + 边界）”** 按钮（id=`run-demo`）
会用小样例依次跑一个正常场景和一个边界场景，目标 15 秒内结束，
两个结果都保留在“历史运行记录”中；更完整的验收场景在页面下方
“完整验收场景选择”里逐个载入运行。

如需手动准备环境（例如 CI）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt      # 运行依赖
pip install -r requirements-dev.txt  # 测试依赖（含 pytest、playwright）
python3 -m playwright install chromium  # 仅浏览器 E2E 需要
```

## 二、测试命令

```bash
# 1) 生成虚构数据与独立真值（data/ 目录）
python3 generator/generate.py --out data

# 2) 单元 + 集成 + 真实 HTTP 场景端到端（共 50+ 断言，无需浏览器）
python3 -m pytest tests/ -q --ignore=tests/test_browser_e2e.py

# 3) Playwright 浏览器端到端（会真实启动 app.py 操作中文页面）
python3 -m pytest tests/test_browser_e2e.py -q

# 全量
python3 -m pytest -q
```

## 三、目录结构与职责分离

```
.
├── app.py                      # 统一启动入口（自动 venv 引导 + 参数解析）
├── openapi/
│   └── experiments.openapi.yaml   # 限定版 OpenAPI 3.0 文档（唯一接口契约）
├── collector/                  # 采集器（只经公开接口/页面交互取数）
│   ├── loader.py               #   文档加载 + 限定子集/只读校验
│   ├── serializer.py           #   style/explode/deepObject/RFC3986 编码
│   ├── builder.py              #   组装精确请求，不支持组合发送前拒绝
│   └── client.py               #   真实 HTTP 采集（requests）
├── server/                     # 本地源站 + Web 入口
│   ├── origin_api.py           #   /v1 公开只读接口（回显原始请求）
│   ├── echo_parser.py          #   独立的反向语义解析器（与采集器互逆）
│   ├── origin_filters.py       #   源站侧过滤实现（独立第二份语义）
│   ├── web_api.py              #   /api 页面内部任务接口
│   └── app.py                  #   Flask 工厂
├── generator/                  # 样例/真值生成器（与采集器分离）
│   ├── dataset.py              #   24 条确定性虚构实验数据
│   ├── reference_filters.py    #   独立参考过滤语义（真值专用）
│   └── generate.py             #   产出 data/experiments.json 与 ground_truth.json
├── fixtures/scenarios.json     # 验收场景（正常/边界/拒绝，含精确期望请求）
├── data/                       # 生成产物（experiments.json 源站数据；ground_truth.json 仅测试用）
├── web/                        # 中文网页（HTML/CSS/原生 JS）
├── tests/                      # 黄金串断言、往返、集成、HTTP E2E、Playwright E2E
└── docs/protocol-subset.md     # 支持的协议子集正式声明
```

**职责边界（硬性约束）**：

* 采集器只能通过文档声明的公开 HTTP GET 接口取数；
  `tests/test_scenarios_e2e.py::test_collector_never_reads_ground_truth_file`
  会静态检查 `collector/` 包内不得引用 `ground_truth`。
* `data/ground_truth.json` 是独立真值，**仅供测试断言**，源站与采集器都不读它。
* 采集器与生成器分离：生成器不参与请求构造，采集器不参与数据生成。

## 四、支持的协议子集（摘要）

完整声明见 [`docs/protocol-subset.md`](docs/protocol-subset.md)。

* 仅 GET 只读；仅 query/path 参数；标量、标量数组、一层标量对象。
* query：`form(+/- explode)`、`spaceDelimited`、`pipeDelimited`、`deepObject`。
* path：`simple`、`label`、`matrix`（各支持 explode 两种取值）。
* 编码：默认 RFC 3986 严格百分号编码；`allowReserved=true` 时值内
  `, / ? : @ ! $ & ' ( ) * + ;` 原样，结构符 `= # %` 始终编码。
* 空数组/空对象在 query 省略并留痕；path 空值直接拒绝。
* 任何不支持的组合**发请求前明确报错**，绝不悄悄换成逗号拼接。

## 五、验收边界覆盖说明

| 边界 | 场景 id | 期望行为 |
|---|---|---|
| 值内逗号、斜杠 | `acc-form-array-explode-false`、`demo-boundary`、`acc-path-simple` | 逗号→`%2C`、斜杠→`%2F`，分隔符不混淆 |
| 空数组 / 空对象 | `acc-empty-array-object`、`demo-boundary` | query 省略并记录在 `omitted` |
| 重复键 | `demo-normal`、往返测试 | `tags=a&tags=b` 顺序与重复完整保留 |
| 对象嵌套边界 | `acc-reject-nested-object`、`acc-reject-deepobject-array` | 发送前拒绝 |
| 保留字符 | `acc-allowreserved` | `,/:` 原样、`=#` 编码 |
| path 多 style | `acc-path-label-matrix`、`acc-path-simple-hit` | 精确路径串 + 回显语义还原 |
| 文档外参数 / 类型错误 | `acc-reject-unknown-param`、`acc-reject-type-mismatch` | 422，`rejected_before_request=true` |

## 六、已知限制 / 未完成项

* 仅实现上述协议子集；通用 OpenAPI 特性（header/cookie 参数、请求体、
  任意 JSON schema、3.1 新特性等）刻意不支持，遇到即拒绝。
* 源站为单进程 Flask 开发服务器，面向本地练习，未做生产级压测与鉴权。
* 浏览器 E2E 依赖本机 Chromium；未安装时该测试文件会 `skip`，
  其余测试不受影响。
