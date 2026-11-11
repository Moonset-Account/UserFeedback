# SVG 实验图数值采集器

面向**本地公开 SVG 实验图**的数值采集器：只依据屏幕上可见的图形、坐标轴刻度与
文本恢复观测点，**不读取任何隐藏数据对象、私有属性或注释**；支持线性轴、对数轴、
反向轴、嵌套 transform、折线断点与裁剪区域；尺度不足以确定时**拒绝给出假精确值**，
并输出可追溯的像素→数据换算过程与误差范围。

所有样例数据均为程序生成的**虚构数据**，不依赖真实账户、外部业务服务或 Docker。

---

## 1. 快速开始

环境要求：Python 3.11+（仅标准库即可运行采集核心；网页与端到端需要下列依赖）。

```bash
# 1. 安装依赖（也可让 app.py 首次启动时自动创建 .venv 并安装）
python3 -m pip install -r requirements.txt

# 2. 生成虚构样例 SVG 与独立真值（服务首次启动也会自动生成）
python3 build_samples.py

# 3. 启动网页（统一入口，自动使用项目 .venv，无需手动激活）
python3 app.py --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000/
```

- 若 `.venv` 不存在，`app.py` 会自动创建虚拟环境、安装 `requirements.txt`，
  然后用该环境重新执行自身；之后启动直接复用，无需任何手动激活。
- 页面固定按 1280×720 设计，关键操作与结果在首屏，证据细节在下方选项卡展开。
- 点击 **`▶ 运行示例（正常 + 边界）`**（按钮 id 为 `run-demo`）会顺序运行
  一个正常场景（线性轴）和一个边界场景（裁剪区域），两次结果都保留在任务历史中，
  端到端实测整体在 15 秒内完成；更完整的 11 类验收场景在左侧下拉框选择。

命令行直接采集：

```bash
python3 -m collector.cli samples/log_y.svg --out result.json
```

---

## 2. 测试命令

```bash
# 全部测试（单元 + 边界，共 20 个断言，纯本地、秒级完成）
python3 -m pytest tests/test_collector.py tests/test_boundaries.py -q

# Playwright 端到端（首次需要安装浏览器内核）
python3 -m playwright install chromium
python3 -m pytest tests/test_e2e.py -q

# 一次跑全部
python3 -m pytest tests/ -q
```

端到端测试会在随机端口**真实启动 `app.py` 子进程**，用 Chromium 真实点击
`run-demo`、场景选择、上传、重采与换算弹窗；若浏览器内核缺失则自动 skip，
不影响其余测试。

---

## 3. 采集器与真值的隔离原则

```
collector/   采集器：纯标准库，只接受 SVG 文本/文件，不导入 generator、不打开 truth
generator/   样例生成器：虚构数据 + 独立真值，仅供演示与测试
samples/
  *.svg               公开实验图（采集器的唯一输入，也是网页渲染的来源）
  truth/*.json        独立真值，仅 tests/ 读取用于断言，采集器与网页均不读取
runs/        网页任务结果 JSON、上传的 SVG 副本（可在外部修改后重新采集）
```

- 采集器公开接口只有 `collector.collect(svg_text)` 与
  `collector.collect_file(path)`，测试通过这两个接口喂入 SVG，再与独立真值比对。
- 网页服务没有任何路由暴露 `samples/truth/`；采集器代码 grep 不到
  `generator` / `truth` 字样。
- 隐藏元素（`display:none`、`visibility:hidden|collapse`、`opacity:0` 及
  其后代）在建模阶段就被剔除；`data-*`、注释、`<metadata>` 不参与任何计算
  （场景 8 专门放置了隐藏诱饵与私有属性，测试断言它们被无视）。

---

## 4. 支持的 SVG 协议子集

网页右上角“协议子集”按钮可查看同样内容。

**支持**

- 元素：`svg/g/line/polyline/polygon/path/circle/ellipse/rect/text/tspan/
  defs/clipPath/use`
- `path` 命令子集：`M L H V Z`（含小写相对形式）；曲线/弧 `C S Q T A` 显式拒绝
- transform：`matrix / translate / scale / rotate(含旋转中心)`，支持任意层嵌套
- `viewBox`：`none` 与 `xMin/xMid/xMax + YMin/YMid/YMid` 的 `meet/slice`
- 裁剪：`clipPathUnits=userSpaceOnUse` 且子形状为 `rect`；多层 `clip-path`
  按嵌套交集处理（同层多矩形取并集）
- 坐标轴：线性轴、以 10 为底的对数轴、反向轴；刻度=可见短线+可见文本标签，
  轴线靠“刻度附着关系”从网格线中识别，支持轻微旋转的轴
- 标签数字：整数/小数、千分位、科学计数法、百分号、常见单位前后缀（仅剥离不换算）

**不支持（遇到即显式跳过/报错，绝不猜测）**

- 曲线/弧命令、`skewX/skewY` 斜切、`em/pt/%` 等非 px 长度单位
- `objectBoundingBox` 单位或非矩形 `clipPath`（相关元素标记为不可信并跳过）
- `10^n` 上标、分数、日期等非数值刻度标签（该刻度不参与标定）
- 外部 CSS 类样式表驱动的可见性（仅识别内联 `style` 与表现属性）

---

## 5. 什么情况下“拒绝给值”

1. **刻度不足**：某条轴可解析的带标签刻度少于 2 个 → 无法确定尺度，该维度全部阻塞。
2. **超出刻度覆盖域**：点投影到轴线后的像素坐标落在全部已标定刻度范围之外
   （2px 容差）→ 拒绝外推。对数轴上这同时覆盖了“零/负位置”的非法点。
3. **非法对数值**：对数轴标定给出 `log10(v) ≤ 0` 的位置 → 真实对数轴不存在该点。
4. **协议外裁剪/图形**：几何不可信，元素整体跳过并登记原因。
5. **被矩形裁剪区裁掉**：点在屏幕上不可见，只登记画布位置与裁剪层级，不产生观测值。

误差范围：刻度配对与像素量化先验取 σ=1.2px，经标定斜率传播；对数轴先在
log 空间传播再映射回数值空间，因此区间天然非对称。误差保留两位有效数字，
观测值按误差的小数位对齐，不制造多余有效数字。

---

## 6. 目录结构

```
app.py                     统一启动入口（自动 venv 引导）
build_samples.py           样例与真值生成脚本
requirements.txt
collector/
  geometry.py              仿射矩阵、点线几何
  transforms.py            transform 属性解析
  svg_model.py             SVG 渲染树建模（CTM、可见性、裁剪阶段）
  axis.py                  轴/刻度识别、标签配对、线性-对数拟合、域外拒绝
  series.py                系列提取、路径子集解析、断点、标记合并
  engine.py                采集编排、误差传播、证据 JSON、协议声明
  cli.py                   命令行入口
generator/
  svg_build.py             轻量 SVG 构建器
  scenarios.py             11 个虚构场景与独立真值
webapp/
  server.py                Flask 路由（无真值路由）
  static/                  中文网页（index.html / app.css / app.js）
tests/
  test_collector.py        四类轴变换与边界的独立真值断言
  test_boundaries.py       解析器与协议子集边界
  test_e2e.py              Playwright 端到端
```

## 7. 已知边界与如实说明

- 采集器只实现上述声明的协议子集，遇到子集外特性走“显式跳过+原因”而非尽力解析。
- 误差模型使用固定像素先验（1.2px），反映的是刻度配对与量化不确定性，
  不包含原图本身的绘制系统误差；若原图刻度标签本身错误，采集器无法识别。
- 单张图按一条主 X 轴、一条主 Y 轴处理；多副轴/双 Y 轴图不在当前子集内。
