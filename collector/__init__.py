"""采集器 —— 判定器唯一的数据入口。

支持三种采集模式，全部只经公开接口/文件/浏览器交互：
  http     requests 库（HTTP/1.1、GET、HTML/JSON 文本响应）
  file     本地文件读取（样例生成器导出的 HTML）
  browser  Playwright 无头浏览器（HTTP/1.1、GET，保留线路原始 HTML）

不支持/不处理：POST 表单写入、鉴权会话、二进制下载、WebSocket ——
本项目协议子集明确限于 HTTP GET 文本响应与本地文件。

架构约束：本包与源站生成器、独立真值文件完全解耦（由架构测试强制）。
"""

from .fetcher import (
    FetchError,
    fetch,
    fetch_with_browser,
    fetch_with_http,
    fetch_with_file,
)

__all__ = [
    "FetchError",
    "fetch",
    "fetch_with_http",
    "fetch_with_file",
    "fetch_with_browser",
]
