# 公开天文观测窗口 · iCalendar 采集器

一个只做**采集与解析**、不做预约或日程管理的 iCalendar（RFC 5545）采集器。
它从公开 HTTP 接口或本地文件取得虚构天文台发布的 `.ics` 数据，在**有界时间区间**内
展开重复规则与例外，用中文网页时间轴展示每个观测窗口，并解释每个实例的来源
（主规则 / RDATE 加场 / RECURRENCE-ID 移动或取消）。

所有数据均为项目生成的虚构数据，不依赖真实账户、外部业务服务或 Docker。

---

## 1. 环境要求与启动

- Python 3.11+（开发与验证版本：3.11.6）
- 启动命令（端口可自行替换）：

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

首次运行会自动在项目内创建 `.venv` 虚拟环境并安装 `requirements.txt`，
随后用该环境重新执行本进程——**无需手动激活虚拟环境**。
启动后浏览器打开 <http://127.0.0.1:8000/> 即可操作。

网页首屏提供：

- 输入来源（公开 HTTP URL 或本地文件路径）、查询范围、展开规模上限；
- `id="run-demo"` 的**「运行示例」按钮**：依次跑一个正常场景和一个边界场景，
  实测约 0.1 秒内完成（要求 15 秒内），两个结果都保留可切换查看；
- 下拉框另有**完整验收场景**（取消/移动/夏令时/无终止/全日/浮动/不支持规则）；
- 时间轴点击任意实例，展示其 UID、实例身份、来源解释与 ICS 行号证据；
- 来源证据、EXDATE 排除记录、问题清单与历史运行可展开查看；
- 页面按 1280×720 设计，关键操作与结果在首屏可见。

## 2. 安装与测试命令

依赖（`icalendar` 解析协议、`python-dateutil` 展开 RRULE、`tzdata` 兜底时区库）：

```bash
python3 -m pip install -r requirements.txt   # 通常由 app.py 自动完成
```

运行全部测试（单元断言 + 端到端，共 18 个）：

```bash
python3 app.py --test
# 或在已激活的环境中：
.venv/bin/python -m unittest discover -s tests -v
```

单独（重新）生成虚构样例与独立真值：

```bash
.venv/bin/python -m samples.generator --out generated
```

## 3. 支持的 iCalendar 协议子集

| 类别 | 支持 | 不支持（遇到即结构化上报，不静默忽略） |
| --- | --- | --- |
| 组件 | `VCALENDAR`、`VEVENT`、`VTIMEZONE`（含 STANDARD/DAYLIGHT） | `VTODO`、`VJOURNAL`、`VAVAILABILITY` 等 |
| 时间语义 | 全日事件（DATE）、浮动时间（无 TZID 的 DATE-TIME）、显式 TZID、UTC、跨日时长 | 不替浮动时间擅自附加时区 |
| RRULE 频率 | `DAILY` / `WEEKLY` / `MONTHLY` / `YEARLY` | `SECONDLY` / `MINUTELY` / `HOURLY` |
| RRULE 部件 | `INTERVAL`、`COUNT`、`UNTIL`、`WKST`、`BYDAY`（含序号如 `-1FR`）、`BYMONTHDAY`、`BYMONTH`、`BYSETPOS` | `BYYEARDAY`、`BYWEEKNO`、`BYHOUR`、`BYMINUTE`、`BYSECOND`、RFC 7529 的 `RSCALE`/`SKIP` |
| 其它规则 | `RDATE`（DATE/DATE-TIME）、`EXDATE`、`RECURRENCE-ID` 移动与 `STATUS=CANCELLED` 取消、同 ID 以 `SEQUENCE` 最大者为准 | `RDATE` 的 PERIOD 值、`RECURRENCE-ID` 的 `RANGE` 参数、单事件多条 RRULE、COUNT 与 UNTIL 同时出现 |

每条未支持规则都会产生带**问题代码、UID 与 ICS 行号**的告警（如 `RRULE_HOURLY`、
`RRULE_BY_YEAR_DAY`），网页问题清单与 API 返回的 `issues` 中均可查看。

## 4. 关键设计与验收点对照

- **有界展开**：所有 RRULE（含无终止重复）只在查询区间 `[起, 止)` 内展开，
  并有 `max_instances`（默认 5000）硬上限，触顶产生 `RANGE_LIMIT` 告警。
- **实例身份**：身份为 `UID :: RECURRENCE-ID 键`，不是开始时间。
  不同 UID 即便开始时间相同也各自保留；同 UID 同身份只保留一条。
- **例外替换不残留**：被 `RECURRENCE-ID` 接管的主规则实例不会再产出常规副本；
  存储按目标名**整体替换**（临时文件 + 原子改名），修改例外后重新采集，
  旧实例不会残留（`tests/test_identity.py` 专门断言）。
- **取消/移动**：取消以 `STATUS=CANCELLED` 实例保留在时间轴（带删除线）；
  移动保留原 `RECURRENCE-ID` 身份、采用例外的新开始时间。
- **夏令时**：icalendar 自带的 pytz 风格 tzinfo 在 dateutil 生成新日期时不会重算
  DST 偏移，采集器统一转换为标准库 `zoneinfo`（保留同一 UTC 瞬间），
  保证墙上时间不变而 UTC 偏移随夏令时正确变化。
- **来源证据**：记录来源 URL/路径、获取方式（http/file）、字节数、Content-Type、
  每个 VEVENT 在原 ICS 中的起止行号与属性行号。
- **采集器与样例分离**：`samples/generator.py` 只负责造数；`collector/` 不导入
  `samples`。独立真值位于 `generated/truth/`，**只有测试读取**，采集器与源站路由
  均不暴露它；测试通过公开采集入口取结果后再与真值比对。
- **规定接口**：采集仅允许 `http(s)` 与本地文件（`file://` 或路径），
  其它协议（如 `ftp`）返回 `UNSUPPORTED_SCHEME`。

## 5. 目录结构

```
app.py                  统一入口（自动建虚拟环境、装依赖、启动服务/跑测试）
requirements.txt
collector/
  fetch.py              采集层（仅 http(s)/file，含大小上限与来源证据）
  expand.py             解析 + 有界展开 + 例外合并 + 行号证据
  timeutil.py           全日/浮动/显式时区归一化、实例身份键、zoneinfo DST 处理
  service.py            任务编排、问题结构化、协议子集声明
  store.py              按目标整体替换的 JSON 存储与运行历史
  errors.py             问题代码（协议子集负面清单）
samples/
  generator.py          虚构 ICS 源 + 独立真值生成器（与采集器分离）
web/                    前端（原生 HTML/CSS/JS，无框架）
webapp/server.py        标准库 HTTP 服务：网页、JSON API、本地公开源站 /source/
tests/                  独立预期断言与端到端验证
generated/              运行期生成（git 忽略）：source/ ics、truth/ 真值、store/ 结果
```

## 6. HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/`、`/static/*` | 中文网页与静态资源 |
| GET | `/source/<name>.ics` | 本地公开源站（仅暴露生成的 ICS，含路径穿越防护） |
| GET | `/api/presets` | 样例预设列表 |
| POST | `/api/collect` | 运行一次采集（body：source、range_start、range_end、max_instances、target_name） |
| POST | `/api/run-demo` | 依次运行正常 + 边界两个小样例 |
| GET | `/api/runs`、`/api/runs/<id>`、`/api/targets` | 历史运行与目标结果 |

## 7. 已知边界（如实说明）

- 未引入浏览器自动化：本项目数据源是静态 ICS，标准库 HTTP 客户端已完整覆盖
  采集与端到端验证，因此没有引入 Playwright；若未来源站改为 JS 动态渲染，
  再按要求引入 Playwright，且仅用于浏览器交互层。
- 查询范围边界为 naive 时间时与浮动时间同尺度比较；不会为浮动时间推断时区。
- RRULE 中 `DTSTART` 与规则不一致等极端容错未做额外修补，解析异常会以
  `BAD_DATETIME` / `PARSE_ERROR` 结构化上报。
