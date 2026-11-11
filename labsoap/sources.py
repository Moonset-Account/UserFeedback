"""规定的采集输入来源：本地内置服务的 HTTP 接口、或受白名单目录约束的样例文件。

采集器只能经这两个公开入口取得原始响应，不读取任何“真值”文件。
"""
from __future__ import annotations

from pathlib import Path

import requests

from .errors import CollectorError, ERR_HTTP, ERR_UNKNOWN_SOURCE

# 文件来源只能落在项目生成的 samples 目录（由 app 启动时生成）
ALLOWED_FILE_ROOT = Path(__file__).resolve().parent.parent / "samples"


def fetch_http(url: str, *, timeout: float = 8.0) -> dict:
    """经公开 HTTP 接口获取原始 SOAP/MTOM 响应字节。"""
    if not url.lower().startswith(("http://", "https://")):
        raise CollectorError(ERR_UNKNOWN_SOURCE, f"非法 HTTP 来源：{url}")
    try:
        resp = requests.post(url, timeout=timeout, headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "urn:fictional-lab-instrument:QueryMeasurements",
        }, data=b'<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"/>')
    except requests.RequestException as exc:
        raise CollectorError(ERR_HTTP, f"请求 {url} 失败：{exc}") from exc
    if resp.status_code >= 400:
        # 500 + SOAP Fault 是合法协议响应，交给上层解析；其它 4xx 才是获取失败
        if resp.status_code != 500:
            raise CollectorError(ERR_HTTP, f"HTTP {resp.status_code}：{resp.reason}")
    return {
        "raw": resp.content,
        "content_type": resp.headers.get("Content-Type", ""),
        "http_status": resp.status_code,
        "source_kind": "http",
        "source_ref": url,
    }


def fetch_file(filename: str) -> dict:
    """读取 samples 白名单目录内的样例 MIME 文件（禁止路径穿越）。"""
    ALLOWED_FILE_ROOT.mkdir(parents=True, exist_ok=True)
    root = ALLOWED_FILE_ROOT.resolve()
    candidate = (root / filename).resolve()
    if root not in candidate.parents and candidate != root:
        raise CollectorError(ERR_UNKNOWN_SOURCE, "文件来源必须位于项目 samples 目录内")
    if not candidate.is_file():
        raise CollectorError(ERR_HTTP, f"样例文件不存在：{filename}")
    data = candidate.read_bytes()
    return {
        "raw": data,
        "content_type": "",
        "http_status": None,
        "source_kind": "file",
        "source_ref": str(candidate.relative_to(root.parent)),
    }
