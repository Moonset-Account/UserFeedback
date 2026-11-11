# 公开实验 ZIP 选择性采集系统

只通过 HTTP `Range` 请求，从公开的实验 ZIP 数据包中**选择性采集用户指定的成员**，
解析并校验其中的虚构实验记录。系统不是通用解压或文件管理工具：它先读中央目录，
再定位所需的本地文件头与压缩数据，全程核对一致性、CRC、大小与对象版本。

## 一、明确声明支持的协议子集

参考 PKWARE APPNOTE.TXT 6.3.x，仅支持以下明确子集（见 `src/selective_zip/protocol.py`）：

- 容器：**单卷、未加密**的 ZIP 与 ZIP64；
- 压缩方式：仅 `stored`(0) 与 `deflate`(8，RFC 1951 原始 DEFLATE)；
- 提取所需版本：≤ 4.5（版本字段 ≤ 45）；
- 文件名编码：传统 CP437 与 UTF-8（通用位标志 bit 11）；
- 数据描述符：支持通用位标志 bit 3（含/不含 `PK\x07\x08` 签名）；
- 目录定位：EOCD、ZIP64 EOCD Locator、ZIP64 EOCD Record；
- 完整性：CRC-32、压缩/未压缩大小、本地头与中央目录一致性。

**明确拒绝**：任何加密（bit 0）、分卷/多卷归档、其他压缩方式、更高提取版本。
拒绝时任务失败且不产生任何正式实验记录。

## 二、采集流程（每一步都是显式 Range 请求）

1. `HEAD` 探测总大小、ETag、`Accept-Ranges`；不支持 Range 直接拒绝；
2. 从文件尾部小窗口定位 EOCD，ZIP64 沿 Locator/Record 定位中央目录；
3. 读取中央目录区间，解析全部条目并建立成员索引；
4. 包级子集检查（加密/分卷整包拒绝），选中成员的压缩方式/版本逐项检查；
5. 逐成员读取本地文件头并与中央目录核对（文件名、标志、压缩方式、版本、CRC、大小）；
6. 按中央目录声明的压缩大小读取压缩数据；bit 3 再单独读数据描述符核对；
7. 解压并核对 CRC-32 与未压缩大小；
8. 记录级校验（对象版本、完整性、实验 ID、观测计数），不合格成员拒绝入册；
9. 全程 ETag 指纹校验，源包一旦变化立即中止，已得结果全部作废。

Range 客户端带区间缓存：重叠/相邻字节只请求一次，保证同一字节绝不重复下载。
网页会展示每一次实际请求的字节区间、状态码、Content-Range、ETag 与流量占比。

## 三、目录结构

```
app.py                     统一启动入口
generate_samples.py        样例生成器独立入口（与采集器分离）
requirements.txt           运行期依赖（核心零第三方依赖，仅用标准库）
requirements-dev.txt       测试依赖（pytest、playwright）
src/
  selective_zip/           采集器核心（Range 客户端、ZIP/ZIP64 解析、编排、记录校验）
  sampler/                 样例生成器（虚构数据、独立 ZIP 写入器、字节级篡改、场景）
  source_site/             本地公开源站（Range/ETag/HEAD、源包变化模拟端点）
  web/                     中文网页与 JSON API（标准库 http.server）
    static/                index.html / styles.css / app.js
tests/                     单元 + 验收 + Playwright 端到端测试
.runtime/sample_data/      运行时生成的样例包与 truth.json（不入库）
```

采集器与生成器是两套独立实现（解析器与写入器不共享 ZIP 代码），避免"自测自"。
`truth.json` 是独立真值，**仅供测试断言**，采集器代码路径完全不读取它；
采集器只能通过源站的公开 HTTP 接口、网页交互获取数据。

## 四、安装与启动

要求 Python 3.11+。运行期零第三方依赖，无需手动激活虚拟环境：

```bash
python3 app.py --host 127.0.0.1 --port 8000
```

启动时会自动在 `.runtime/sample_data` 生成全部虚构样例（已存在则复用），
随后启动本地公开源站（随机端口）与网页（指定端口），浏览器打开根路径即可操作。
加 `--regenerate` 可重新生成样例，`--no-browser` 可禁止自动开浏览器。

## 五、网页操作

- 左侧填写 ZIP 公开地址、实验 ID、要采集的成员名（每行一个），点"运行采集任务"；
- **"运行示例"按钮（id=`run-demo`）**：用小样例依次运行一个正常场景
  （`normal_small`）和一个边界场景（`crc_bad` CRC 错误），实测约 0.1 秒内结束，
  两个结果都保留，可在右侧标签切换查看；
- 下方"完整验收场景"列出 11 个场景，点"使用此场景"即填充配置，可单独运行；
- 右侧展示：成功/失败摘要、流量证据、**实际请求字节区间表**、正式实验记录、
  被拒绝成员、具体错误代码与说明、执行日志；细节证据默认折叠可展开。
- 页面按 1280×720 适配，关键操作与结果在首屏可见。

## 六、验收场景（全部由生成器产出，虚构数据）

| 场景 | 预期 |
| --- | --- |
| `normal_small` | 6 个成员只取 2 个，正常入册 |
| `normal_big` | 20 成员大包只取 2 个，实际下载 < 整包 25% |
| `normal_stored` | 全 stored 成员正常采集 |
| `normal_zip64` | 强制 ZIP64 结构，经 Locator/Record 定位 |
| `bit3_descriptor` | bit3 数据描述符，本地头 CRC/大小为 0 |
| `crc_bad` | 压缩数据被篡改 → CRC/解压失败，任务失败、无记录 |
| `header_conflict` | 本地头与中央目录冲突 → 任务失败、无记录 |
| `mixed_versions` | 跨版本/不完整/计数不符/实验不符成员被拒绝，好成员入册 |
| `encrypted` | 加密包在子集检查阶段整包拒绝 |
| `multivolume` | 分卷包在 EOCD 阶段整包拒绝 |
| `mutation` | 采集过程中源包切换，ETag 变化触发中止、无记录 |

## 七、测试

```bash
# 测试依赖（仅测试需要，运行 app.py 不需要）
python3 -m pip install -r requirements-dev.txt
python3 -m playwright install chromium

# 全部测试（45 个：单元 + 验收 + Web API + 浏览器 E2E）
python3 -m pytest -q

# 只跑非浏览器测试
python3 -m pytest tests/test_range_client.py tests/test_zip_structs.py \
  tests/test_records.py tests/test_fetch_acceptance.py tests/test_web_api.py -q

# 隔离模式（推荐，模拟不受现场环境污染的解释器）
python3 -I -m pytest -q
```

未安装 Playwright/Chromium 时浏览器用例会自动 skip，HTTP 层验收仍完整覆盖。

## 八、设计边界（不做什么）

- 不提供整包下载、目录浏览、上传或文件管理能力；
- 不做通用解压工具，仅解析选定成员并校验声明的 JSON 记录；
- 不接受源站对 Range 请求返回 200（整包），从协议上杜绝"假装选择性采集"；
- 任何不完整或跨版本成员都不会进入正式实验记录。
