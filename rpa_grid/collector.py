"""采集器：只经规定的公开接口获取实验矩阵。

采集边界（强约束）：
- 仅允许 HTTP GET 同源路由 ``/samples/<scenario>/matrix.tsv``，
  以及显式登记的本地文件路径（测试时用 file:// 注入）；
- 以字节读取后 UTF-8 解码，禁用 Python 通用换行，保证 ``\\r``/``\\t`` 保真；
- 禁止读取 truth.json——本模块不包含任何对 truth 的引用；
- 同时抓取 grid_config（同样经公开接口），供规划阶段识别只读/隐藏/冻结。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urljoin

from .matrix import ExperimentMatrix, GridConfig, build_matrix_from_tsv


class CollectorError(RuntimeError):
    """采集失败（网络、编码、协议错误统一包装）。"""


@dataclass
class CollectedBundle:
    matrix: ExperimentMatrix
    grid_config: GridConfig
    evidence: Dict[str, str]  # 接口 URL -> 响应片段指纹，供网页展示来源证据


def _http_get_bytes(url: str, timeout: float = 10.0) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise CollectorError(f"接口返回非 200 状态: {resp.status}")
            return resp.read()
    except CollectorError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一包装为采集错误
        raise CollectorError(f"GET {url} 失败: {exc}") from exc


def _read_bytes(uri: str) -> bytes:
    if uri.startswith(("http://", "https://")):
        return _http_get_bytes(uri)
    if uri.startswith("file://"):
        return Path(uri[len("file://"):]).read_bytes()
    raise CollectorError(
        f"未登记的采集来源: {uri!r}；只允许同源 HTTP 接口或 file:// 测试注入"
    )


def collect(base_uri: str, scenario: str) -> CollectedBundle:
    """从公开接口采集一个场景的矩阵与网格配置。

    base_uri 例如 http://127.0.0.1:8765
    """
    tsv_url = urljoin(base_uri.rstrip("/") + "/", f"samples/{scenario}/matrix.tsv")
    cfg_url = urljoin(base_uri.rstrip("/") + "/",
                      f"samples/{scenario}/grid_config.json")

    tsv_bytes = _read_bytes(tsv_url)
    cfg_bytes = _read_bytes(cfg_url)

    try:
        tsv_text = tsv_bytes.decode("utf-8")  # 字节解码，保留 \r
    except UnicodeDecodeError as exc:
        raise CollectorError(f"{tsv_url} 不是合法 UTF-8: {exc}") from exc

    try:
        cfg_dict = json.loads(cfg_bytes.decode("utf-8"))
        grid_config = GridConfig(**cfg_dict)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CollectorError(f"网格配置解析失败: {exc}") from exc

    try:
        matrix = build_matrix_from_tsv(
            tsv_text, scenario=scenario, source_ref=tsv_url
        )
    except Exception as exc:  # 协议/建模错误同样归为采集失败
        raise CollectorError(f"矩阵构建失败: {exc}") from exc

    evidence = {
        "matrix_tsv_url": tsv_url,
        "matrix_bytes": str(len(tsv_bytes)),
        "matrix_sha256_12": _fingerprint(tsv_bytes),
        "grid_config_url": cfg_url,
    }
    return CollectedBundle(matrix=matrix, grid_config=grid_config,
                           evidence=evidence)


def _fingerprint(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:12]
