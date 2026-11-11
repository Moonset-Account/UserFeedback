"""协议后端接口定义。

声明支持的协议子集：
  - HTTPBackend   : HTTP/1.1 + HTML(锚点/JSON-LD 式同源 JSON) 解析 + 二进制下载
  - PlaywrightBackend: Chromium 自动化，等待 JS 渲染后读取 DOM，再经浏览器上下文下载
后端只负责“取得地址 / 发起下载”的原始交互，不做业务判断与刷新决策。
"""
from __future__ import annotations

from typing import Protocol

from collector.models import AcquiredLink, DownloadResponse


class BaseBackend(Protocol):
    name: str

    def acquire_link(self, page_url: str) -> AcquiredLink:
        """打开实验页面，返回短时下载地址、业务版本、实验编号与来源证据。"""
        raise NotImplementedError

    def download(self, url: str) -> DownloadResponse:
        """发起一次下载尝试，原样保留状态码、响应头与字节内容。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放底层资源（连接池 / 浏览器进程）。"""
        raise NotImplementedError
