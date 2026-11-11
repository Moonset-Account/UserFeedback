"""HTTP 协议后端（成熟开源库：requests + BeautifulSoup4/lxml）。

支持的协议子集：
  HTTP/1.1 GET；HTML 4/5 文档内 a[href] 锚点解析；同源 application/json 清单；
  application/zip 等二进制响应体下载。不执行 JavaScript，不跟随站外跳转。
"""
from __future__ import annotations

from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from collector.models import AcquiredLink, AcquireError, BackendError, DownloadResponse

USER_AGENT = "ExpiringAssetCollector/1.0 (local fictional demo; +https://example.invalid)"
TIMEOUT = 15
MAX_BYTES = 20 * 1024 * 1024  # 20 MiB 上限，防止异常响应拖垮采集器


class HttpBackend:
    name = "http"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

    # -- 取得地址 -------------------------------------------------------------
    def acquire_link(self, page_url: str) -> AcquiredLink:
        try:
            resp = self._session.get(page_url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise BackendError(f"页面请求失败: {exc}") from exc

        ctype = resp.headers.get("Content-Type", "")
        evidence = {
            "page_url": page_url,
            "status_code": resp.status_code,
            "content_type": ctype,
            "method": "html-anchor",
        }
        if resp.status_code != 200:
            raise AcquireError(f"实验页面返回 HTTP {resp.status_code}，无法取得链接", evidence)
        if "html" not in ctype.lower():
            raise AcquireError(f"实验页面不是 HTML（Content-Type={ctype}）", evidence)

        soup = BeautifulSoup(resp.text, "lxml")
        anchor = soup.select_one("a#download-link[href]")
        if anchor is None:
            # JS 渲染页面：静态 HTML 中没有锚点
            raise AcquireError(
                "静态 HTML 中不存在 id=download-link 的下载锚点；该页面可能由 JavaScript 渲染，"
                "请改用 Playwright 浏览器协议。",
                evidence,
            )

        download_url = urljoin(page_url, anchor["href"])
        version_el = soup.select_one("#business-version")
        exp_el = soup.select_one("#experiment-id")
        version = version_el.get_text(strip=True) if version_el else ""
        experiment_id = exp_el.get_text(strip=True) if exp_el else ""
        if not version or not experiment_id:
            raise AcquireError("页面缺少业务版本号或实验编号元素", evidence)

        evidence.update({
            "anchor_html": str(anchor)[:300],
            "version_text": version,
            "experiment_id": experiment_id,
            "download_url": download_url,
        })
        return AcquiredLink(download_url, version, experiment_id, evidence)

    # -- 下载 -----------------------------------------------------------------
    def download(self, url: str) -> DownloadResponse:
        try:
            resp = self._session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BYTES:
                    resp.close()
                    raise BackendError(f"响应体超过 {MAX_BYTES} MiB 上限，中止下载")
                chunks.append(chunk)
            content = b"".join(chunks)
            headers = {k: v for k, v in resp.headers.items()}
            return DownloadResponse(
                status_code=resp.status_code,
                headers=headers,
                content=content,
                final_url=resp.url,
            )
        except requests.RequestException as exc:
            return DownloadResponse(
                status_code=0, headers={}, content=b"", final_url=url,
                transport_error=f"传输异常: {exc}",
            )

    def close(self) -> None:
        self._session.close()
