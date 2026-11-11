# RPA 实验步骤依赖重排平台

依据实验步骤的**先后依赖**对本地“流程编辑页”进行自动重排的 RPA 项目。页面包含
固定锚点步骤、可拖动步骤和同组相对顺序约束；系统先判断约束是否可满足，再生成
移动次数最少的确定性计划，通过 Playwright 拖拽执行并在**每一步回读真实顺序**；
若源页顺序被用户手动改变，立即按最新回读顺序重新比较、重新规划，绝不复用过期下标。

全部数据为虚构实验数据，不依赖真实账户、外部业务服务或 Docker。

---

## 1. 快速开始

要求 Python 3.11+。统一入口：

```bash
python3 app.py --host 127.0.0.1 --port 8765
```

然后浏览器打开 <http://127.0.0.1:8765/> 即可操作。

- 若当前解释器已具备 `requirements.txt` 中的依赖（本机即如此），直接启动；
- 若缺少依赖，入口会**自动**在项目内创建 `.venv`、安装依赖并安装 Playwright
  Chromium，然后用该环境重新执行自身——无需手动激活虚拟环境。
- 页面右上角 **“运行示例”按钮（id=`run-demo`）** 会依次运行一个正常场景和一个
  循环依赖边界场景，实测约 2 秒结束（要求 15 秒内），两个结果都保留在任务记录中；
  更完整的 10 步综合验收场景（`full-acceptance`）在左侧下拉框中选择运行。

### 依赖安装（手动方式，可选）

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

依赖：Flask（本地源站与控制台）、jsonschema（协议结构校验）、
beautifulsoup4（HTML 子集解析）、playwright（浏览器自动化）、pytest（测试）。

## 2. 测试命令

```bash
# 全部 37 个测试（单元 + 采集器隔离 + Playwright 端到端），约 8 秒
python3 -m pytest -q

# 仅纯算法/协议单元测试（不需要浏览器）
python3 -m pytest tests/test_planner.py tests/test_parser.py -q

# 仅端到端（真实 Chromium 拖拽）
python3 -m pytest tests/test_e2e.py -q
```

## 3. 协议子集声明：`rpa-order/v1`（JSON）

解析器 `core/parser.py` 使用成熟开源库 **jsonschema** 做结构校验，**仅支持以下子集**：

| 范围 | 支持内容 |
|---|---|
| 顶层字段 | `protocol`（必须等于 `rpa-order/v1`）、`scenario_id`、`title`、`steps`、`constraints`、`source`、`interference`；未知顶层字段直接报错 |
| `steps` | 1–64 个步骤；`id` 匹配 `^[A-Za-z0-9_-]{1,32}$` 且唯一；`name` 必填；`group` 可选 |
| `constraints.fixed` | 步骤 id → 固定槽位（0 起，越界报错） |
| `constraints.depends_on` | 步骤 id → 直接前置步骤 id 列表；传递关系由求解器推导，不允许引用不存在的步骤 |
| 同组约束 | 相同 `group` 的步骤必须保持其在 `steps` 中的原始相对顺序，解析器将其展开为依赖边 |
| `interference` | 测试专用：`after_moves` 次移动后交换 `swap_a/swap_b`，模拟用户手动换位 |

**不支持**并会被拒绝：软约束/权重/互斥、跨文件引用、YAML/TOML 等其他编码、
未知字段（`additionalProperties: false`）。
HTML 采集通道（BeautifulSoup）的子集更小：仅识别 `#step-list` 下带
`data-step-id` 的 `<li>` 元素。

## 4. 约束求解与规划算法（`core/planner.py`，纯函数）

1. **建图**：`depends_on` 与同组展开边构成有向图。
2. **可满足性判断（先判断再规划）**：
   - DFS 环检测，环上输出完整**循环冲突链**（如 `X -> Y -> Z -> X`）；
   - 固定槽位抢占（两个步骤抢同一槽位）；
   - 固定步骤之间依赖方向与槽位顺序矛盾（附 BFS 具体路径链）；
   - 固定步骤的传递前置/后继数量超过槽位容量。
   不可满足时抛出 `UnsatisfiableError`，`chains` 为人类可读的具体冲突链，
   不会生成任何移动计划。
3. **确定性目标序**：固定槽位门控的 Kahn 拓扑排序——固定步骤只有在其槽位
   到达时才允许放置，平局按原始下标，结果可重复。
4. **最少移动计划**：对当前顺序与目标序求**加权 LCS**（固定锚点权重 100，
   优先保留锚点不动），保留子序列外的步骤数 `n-LCS` 即任何方案的移动次数
   下界；本项目按目标序反向、以“最近的 LCS 保留前驱”为锚点构造拖拽，
   移动次数**恒等于该下界**，并在计划诊断中声明是否达到最优。

## 5. 执行器与“过期下标”防护（`rpa/executor.py`）

- 每一轮都重新从 DOM（`#step-list li[data-step-id]`）回读当前顺序，再调用
  `plan_moves` 重新规划；执行器不缓存任何下标。
- 记录“自身上一动作的预期顺序”，若回读偏离，判定为用户手动换位，计一次
  **重规划**并作废旧计划（事件日志含预期/实际两份顺序）。
- 每次拖拽后立即回读，与纯函数推导的期望顺序比对，不符则重试，再失败如实报错。
- 固定步骤若被用户拖离槽位，重新规划会生成把它拖回锚点槽位的移动。
- `interference` 仅用于测试：通过页面自身公开的 `window.moveCard` 模拟用户
  手动换位，事件在界面与证据中标注为“用户手动换位”。

## 6. 采集器与样例生成器分离（硬性架构约束）

```
generator/   样例生成器：写 samples/（公开输入）与 samples/truth/（独立真值）
collector/   采集器：只走三条公开通道，不导入 planner/verifier/executor
  ├─ collect_from_file   读 rpa-order/v1 文件；访问 truth/ 目录直接抛 CollectionError
  ├─ collect_via_http    GET 本地源站 /api/scenario/<id>
  └─ collect_dom_order   Playwright 打开页面，从公开 DOM 回读顺序
core/        协议解析、约束求解、规划、独立验证（彼此独立）
rpa/         Playwright 执行器与任务编排
web/         Flask 控制台 + 本地源站流程编辑页（HTML/CSS/JS，无前端框架）
tests/       单元断言（含对 truth 真值的独立断言）与端到端验证
runs/        每次任务的落盘证据（运行后生成）
```

`samples/truth/` 仅供 pytest 使用，采集器在文件系统层面拒绝读取该目录
（`tests/test_collector.py::test_collector_refuses_truth_directory` 守护）。

## 7. 验收场景对照

| 验收要求 | 场景/测试 |
|---|---|
| 多个合法排序 | `normal-titration`；`test_multiple_valid_orders_choose_deterministic` |
| 固定步骤冲突 | `fixed-conflict`（同槽位抢占 + 依赖方向矛盾），输出具体冲突链 |
| 循环依赖 | `cyclic-deps`（X→Y→Z→X），输出完整环链 |
| 执行中手动换位 | `manual-swap`（5 步）、`full-acceptance`（10 步，含干扰） |
| 不满足约束给冲突链 | `UnsatisfiableError.chains`，网页“具体错误/冲突链”面板 |
| 成功时独立验证 | `core/verifier.py` 三项检查：排列完整性、依赖与同组方向、固定槽位 |
| 来源证据与错误可见 | 每次移动的移动前/回读顺序、重规划事件、`runs/<run_id>.json` |

## 8. 目录与数据说明

- 所有样例均为虚构的滴定/接种/色谱实验步骤；
- 页面适配 1280×720，关键操作与结果首屏可见，完整证据在底部可展开区域；
- 结果全部来自真实 Playwright 执行，任何任务都不预填成功状态。
