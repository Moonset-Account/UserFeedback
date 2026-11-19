# 口令筛查服务与客户端（本地可测）

一个 k-anonymity 风格的**前缀/后缀**口令筛查实现：

- **服务端**持有一份合成、定长（SHA-256，64 位十六进制）的摘要清单，按版本发布。
- **客户端**只把摘要**前缀**、页码和固定版本号发给服务端；拿回整页后缀后在本地比对，口令与完整摘要都不出客户端。
- **一个版本内所有前缀（包括空桶）返回相同的页数与相同的每页条数**；不足一页的部分用**独立类型**的填充项补齐，填充项永远不会成为真实命中。
- **整版原子切换**：一次更新整体替换当前版本。版本的最满桶跨过页边界时，页数会变化，但变化只对新版本生效。
- **翻页中的旧查询**：要么完整读完它pin住的旧版，要么收到明确的 `restart_required` 信号并从头重来——绝不把两版的页拼在一起判断一个口令。

所有实现、测试与示例都只在本地运行，清单为合成夹具，不做任何外部网络访问。

## 目录

```
password_screening/
  digest.py    摘要规范化、SHA-256 派生、前缀/后缀切分
  version.py   DigestVersion：不可变版本、统一分页、record/padding 条目
  store.py     VersionStore：保留最近 N 版、原子发布、过期版本 -> restart_required
  server.py    ScreeningService（JSON 请求/响应）+ loopback HTTP 前端
  client.py    ScreeningClient（本地比对后缀、版本锁定、重启重试）+ 两种 transport
  errors.py    错误类型（InvalidPrefix / PageOutOfRange / RestartRequired / ...）
  __main__.py  CLI：screen / serve / probe
examples/
  demo.py      进程内 + HTTP 端到端演示
  digests.txt  合成摘要清单（示例用）
tests/
  test_uniform_pagination.py  统一页数/页大小、空桶、填充类型与不可命中
  test_version_switch.py      原子切换、旧版读完、显式重启、并发发布
  test_boundary_crossing.py   最满桶跨页边界的影响
  test_protocol.py            前缀私密性、输入拒绝、HTTP 契约
  test_cli.py                 命令行入口真实断言
```

## 入口

Python 3.11（仓库内 `.venv`，无需额外依赖）：

```bash
# 全量验收用例（真实断言）
.venv/bin/python -m unittest discover -s tests -t . -v

# 服务端 + 客户端进程内演示
.venv/bin/python examples/demo.py

# CLI：本地筛查（摘要文件 + 口令，逐条输出 JSON）
.venv/bin/python -m password_screening screen \
    --digests-file examples/digests.txt \
    --password hunter2 --password not-on-the-list

# CLI：起一个 loopback HTTP 服务
.venv/bin/python -m password_screening serve \
    --digests-file examples/digests.txt --port 8765

# CLI：查询已运行的服务
.venv/bin/python -m password_screening probe \
    --url http://127.0.0.1:8765 --password swordfish
```

`screen` 在命中且带 `--fail-on-match` 时返回退出码 1，否则 0。

## 协议要点

请求（`POST /lookup`，JSON）：

```json
{"op": "page", "prefix": "F52FB", "page": 0, "version": "5812237753c2204f"}
```

- `prefix` 必填，长度必须等于服务的 `prefix_length`，仅十六进制；大小写会被规范化。
- `version` 可选。首包不填时由服务端返回的 `served_version` 决定，后续每页都锁定该值。
- 只允许 `page` / `info` 两种 `op`。

响应：

```json
{"ok": true, "op": "page", "served_version": "...", "prefix": "F52FB",
 "page": 0, "page_size": 8, "total_pages": 2,
 "entries": [{"kind": "record", "suffix": "..."},
             {"kind": "padding", "suffix": "..."}]}
```

`entries` 长度恒等于 `page_size`。`kind` 只有 `record`（真实后缀）与 `padding`（诱饵后缀）；客户端只比较 `record`。

失败响应：`{"ok": false, "error": "<code>", ...}`，`code` 取
`invalid_prefix` / `page_out_of_range` / `version_unavailable` /
`no_current_version` / `restart_required` / `invalid_request` / `protocol_error`。
退役版本的分页请求返回 `restart_required: true` 并附带 `current_version`。

## 验证范围

`tests/` 下 50 个用例覆盖以下交叉约束（每类都含"可接受"与"应被拒绝/阻止"两侧）：

| 约束 | 可接受结果 | 应被拒绝/阻止 | 交错/边界场景 |
| --- | --- | --- | --- |
| 统一分页 | 命中桶、短桶、空桶页数与每页条数一致 | 越界页、非法前缀被拒 | 空桶只剩 padding；填充后缀与真实后缀不碰撞 |
| 填充不可命中 | 真实 record 命中 | padding 即使后缀与目标相同也不命中 | 恶意服务把目标后缀塞进 padding |
| 只传前缀 | 请求只含 prefix/page/version | 短摘要、非法前缀、长度不符被拒 | 校验请求体中不含完整摘要、口令、后缀 |
| 整版切换 | 旧查询读完 pin 住的旧版 | 跨页拼接两版时客户端抛 `ProtocolError` | 最满桶刚好整页后被 +1 记录推过页边界 |
| 显式重启 | 版本仍保留时旧查询完整读完 | 版本被淘汰后返回 `restart_required` | 发布两次导致 v1 被淘汰，客户端重挂 v3 后命中 |
| 原子发布 | 并发读取总能看到完整版本 | 混版响应被拒绝 | 4 读 1 写并发下无丢命中、无异常 |

## 已知限制

- 填充后缀由 `version_id` 与位置确定性派生，仅保证同一前缀内不与真实后缀碰撞；它不保证与杀软/泄漏库的真实分布一致（这是合成夹具）。
- `VersionStore` 使用进程内锁，单进程多线程安全；跨进程一致性依赖外部仲裁（本实现不提供）。
- 保留版本数由 `retain` 决定（默认 4，最少 2）；超出后最旧版本被淘汰，其上的在途查询会收到 `restart_required`。
- HTTP 前端只用于本地验证，未实现 TLS、认证或限流。
