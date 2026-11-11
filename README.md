# 科研测量数值采集器（locale-aware measurement collector）

面向科研测量页面的**数值采集器**：从规定的公开接口（本地文件 / HTTP / 浏览器 DOM）
取得测量**原文**，在**显式地区规则**下解析十进制逗号、千分位分组、不换行空格、
科学计数法、正负不确定度、检测限标记与缺失值，逐条保存数值、单位、限定符、
精度信息、来源位置，并给出**逐步解析解释**。

> 全部数据与机构（如“清河环境观测站”）均为**虚构**，仅用于本地非生产演示；
> 不依赖真实账户、外部业务服务或 Docker。

---

## 1. 支持的协议子集（明确声明）

### 1.1 数字与地区规则（CLDR 通用数字格式的常用子集）

| 地区代码 | 小数点 | 分组分隔符 | 说明 |
|---|---|---|---|
| `en_US` / `zh_CN` | `.` | `,` | 点小数、逗号千分位 |
| `de_DE` | `,` | `.` | 逗号小数、点千分位 |
| `fr_FR` | `,` | 不换行空格 `U+00A0`、窄不换行空格 `U+202F`（协议子集额外接受普通空格） | |
| `sv_SE` | `,` | 窄不换行空格 `U+202F`、不换行空格（额外接受普通空格） | |
| `und`（未定） | `.` | 不启用分组 | 歧义一律报错，**绝不猜测** |

- 数字：阿拉伯数字 `0-9`，Unicode 减号 `− – —` 归一为 ASCII `-`；
- 科学计数法：仅 ASCII `e/E`（如 `7.8E-3`、`7,8e-3`）；
- 分组合法性：整数部分首组 1–3 位、其余各组 3 位；小数部分不允许分组符；
- 内部一律使用 `decimal.Decimal`，无浮点误差。

### 1.2 测量语义

- **缺失值**：`-`、`—`、`–`、`−`、`N/A`、`null`、`无数据` 等 → 空值（`null`），
  **永远不会变成 0**；
- **检测限**：`< L` / `> L` → 限定符 `below/above_detection_limit`，`L` 仅为界值，
  **不当作精确等值**，步骤中明确禁止等值比较；
- **区间**：`a – b`、`a ~ b`、`a to b`、`a - b`（带空格连字符）→ 保存上下界，
  不合并为单点；下界大于上界报 `BAD_RANGE`；
- **不确定度**：对称 `x ± u`、括号记法 `x(u)`（按尾数对齐换算）、
  非对称 `x +u1 -u2`；
- **单位**：固定白名单（mg/L、μg/L、pH、°C、kPa、ppb、% 等），
  未知单位报 `ILLEGAL_UNIT`，**不自动换算、不猜测**；
- 每条记录保存：`raw_text`（原文）、`value/value_end`、`unit/unit_raw`、
  `qualifier`、`precision`（小数位数、有效数字）、`uncertainty`、
  `steps`（逐步解释）、`source`（来源类型与位置）。

### 1.3 不在支持范围内（会显式报错而非静默处理）

罗马数字、中文数字、非 `e` 指数记法、单位自动换算、白名单外单位、
无显式地区时的歧义字面量（如 `und` 下的 `1,234`）。

---

## 2. 架构与“采集 / 生成 / 真值”分离

```
app.py                  统一启动入口（自动切换 .venv，无需手动激活）
collector/              采集器（核心逻辑，不读取 samples/truth）
  locales.py            地区规则
  parser.py             数值字面量解析（Decimal + 逐步解释）
  units.py              单位白名单
  extractor.py          测量记录语义（缺失/检测限/区间/不确定度）
  fetchers.py           规定接口：file / HTTP(requests+BeautifulSoup) / 浏览器(Playwright)
  tasks.py              任务编排与同原文跨地区重解释
generator/
  sample_data.py        虚构样例生成器（产出 source/ 与独立 truth/）
web/
  console.py            操作台 Flask 应用与 JSON API
  source_site.py        本地虚构源站（/source 下 HTML/JSON/CSV 公开接口）
  templates/ static/    中文操作页面（HTML/CSS/JS）
samples/
  source/               采集器可读的源文件（生成器产出，可重复生成）
  truth/                独立预期真值，仅供测试断言，采集器代码禁止引用
tests/                  pytest 独立断言 + Playwright 端到端验证
runs/                   任务运行记录（自动持久化）
```

结构性保证：`tests/test_acceptance.py::test_collector_never_reads_truth_directory`
会扫描 `collector/` 源码，确认其中不出现 `truth` 引用。

---

## 3. 安装依赖（Python 3.11+）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # 仅浏览器采集/端到端测试需要
```

---

## 4. 启动与操作

统一入口（脚本检测到 `.venv` 会自动 `exec` 到该解释器，**无需手动激活虚拟环境**）：

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000/> 即可：

1. 选择采集方式（手动文本 / 本地文件 / HTTP / 浏览器 DOM）与**地区规则**；
2. 配置输入后点击「运行采集任务」，或点击 **`id=run-demo` 的「运行示例」**：
   依次运行一个正常场景与一个边界场景（缺失值、检测限、非法单位、歧义），
   小样例端到端在 15 秒内完成，两个场景结果均保留可查看；
3. 「运行完整验收场景」提供 5 种地区规则 × 歧义原文重解释等更完整入口；
4. 每条结果可展开查看**逐步解析解释与来源证据**（文件行列 / JSON Path /
   URL+CSS 选择器 / 表格坐标）；
5. 页面按 1280×720 适配，关键操作与汇总在首屏，其余证据可展开。

虚构源站地址（同进程提供）：

- HTML 页面：<http://127.0.0.1:8000/source/measurements.html>
- JSON：`/source/measurements.json`、CSV：`/source/measurements.csv`
- 德语子集：`/source/de/measurements.json`

重新生成样例（幂等）：

```bash
.venv/bin/python generator/sample_data.py samples
```

---

## 5. 测试命令

```bash
# 全量（含 Playwright 端到端；未安装 Chromium 时浏览器用例自动 skip）
.venv/bin/python -m pytest tests/ -q

# 仅核心解析与验收断言
.venv/bin/python -m pytest tests/test_parser.py tests/test_extractor_units.py tests/test_acceptance.py -q
```

测试覆盖（共 58 项，2026-11-11 本地全部通过）：

- **四种以上格式**：逗号分组、点/逗号小数、不换行空格分组、科学计数法、
  括号/对称/非对称不确定度；
- **空白**：NBSP / 窄 NBSP / 普通空格分组，以及“数字 空格 单位”边界；
- **破折号缺失值**：`-`、`—`、`N/A` 在多个地区下保持 `null`，不变 0；
- **区间**：上下界保存、倒置报错；
- **非法单位**：`zz`、`parsec` 被拒且数值保留；
- **地区切换重解释**：同一原文 `1,234` 在 `und` 报 `AMBIGUOUS_LOCALE`、
  `en_US`→1234、`de_DE`→1.234，原文完全一致；
- **检测限语义**：`< 0.01` 标记为低于检测限，步骤禁止当精确等值；
- 接口链路（文件/HTTP/浏览器 DOM）、独立真值逐行对照、运行记录持久化、
  `run-demo` 按钮 15 秒内完成的 Playwright 端到端断言。

---

## 6. HTTP API 摘要

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/locales` | 可选地区规则及说明 |
| GET | `/api/samples` | 样例文件与源站接口清单 |
| POST | `/api/run` | 运行任务，body：`mode`(text/file/http/browser)、`locale`、`text/path/url` |
| POST | `/api/demo` | 运行示例（正常 + 边界两个场景） |
| POST | `/api/acceptance` | 完整验收场景 |
| GET | `/api/runs`、`/api/runs/<name>.json` | 历史运行记录 |

---

## 7. 已知限制（如实说明）

- 内置为 Flask 开发服务器，面向本地演示，未做生产级并发与鉴权；
- 浏览器采集仅验证 Chromium（Playwright），未覆盖其他内核；
- 单位为固定白名单，不做量纲换算；地区规则为常用子集，未覆盖全部 CLDR 地区；
- 采集器不做任何外部网络调用，HTTP/浏览器目标默认指向本地虚构源站。
