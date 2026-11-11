"""清单抓取层：仅经公开 HTTP(S) 接口或本地文件获取输入。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import requests


class FetchError(Exception):
    """清单无法获取或内容不是合法 JSON。"""


def source_kind(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        return "http"
    return "file"


def fetch_manifest(source: str, timeout: float = 15.0) -> tuple[dict, str]:
    """返回 (manifest_dict, 实际来源 kind)。

    - http/https：GET 请求，只读取公开接口返回的 JSON；
    - 其他输入按本地文件路径处理。
    """
    kind = source_kind(source)
    if kind == "http":
        try:
            resp = requests.get(
                source,
                timeout=timeout,
                headers={"Accept": "application/ld+json;profile=http://iiif.io/api/presentation/3/context.json, application/json"},
            )
        except requests.RequestException as exc:
            raise FetchError(f"HTTP 请求失败：{exc}") from exc
        if resp.status_code != 200:
            raise FetchError(f"HTTP 状态码 {resp.status_code}：{source}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError(f"响应不是合法 JSON：{source}") from exc
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise FetchError(f"本地清单文件不存在：{source}")
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise FetchError(f"本地清单读取/解析失败：{exc}") from exc

    if not isinstance(data, dict):
        raise FetchError("清单顶层必须是 JSON 对象")
    return data, kind
