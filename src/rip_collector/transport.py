"""RIP/1.0 采集传输层：只通过规定的公开接口（HTTP NDJSON 流或 NDJSON 文件）取数。

传输层不理解业务语义，逐行交给 CollectorEngine；传输异常时调用
engine.mark_interrupted()，保证流中断不会把暂存树标成完整。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import httpx

from .engine import CollectorEngine


class TransportError(Exception):
    pass


def _iter_http_lines(url: str, timeout: float) -> Iterator[str]:
    """逐行读取 HTTP NDJSON 流。源站可通过提前关闭连接模拟流中断。"""
    with httpx.stream("GET", url, timeout=timeout) as resp:
        if resp.status_code != 200:
            raise TransportError(f"源站返回 HTTP {resp.status_code}")
        for line in resp.iter_lines():
            if line:
                yield line


def _iter_file_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield line


def run_http_collection(engine: CollectorEngine, url: str,
                        timeout: float = 15.0) -> CollectorEngine:
    try:
        for line in _iter_http_lines(url, timeout):
            engine.ingest_line(line)
    except (httpx.HTTPError, TransportError, OSError) as exc:
        engine.mark_interrupted(f"http_transport: {type(exc).__name__}: {exc}")
        return engine
    if not engine.snapshots[-1]["complete"] and engine.snapshots[-1]["status"] == "streaming":
        # 连接正常结束但没有 end 标志：同样属于未收到结束标志
        engine.mark_interrupted("stream_closed_without_end")
    return engine


def run_file_collection(engine: CollectorEngine, path: str | Path) -> CollectorEngine:
    p = Path(path)
    try:
        for line in _iter_file_lines(p):
            engine.ingest_line(line)
    except OSError as exc:
        engine.mark_interrupted(f"file_transport: {type(exc).__name__}: {exc}")
        return engine
    if engine.snapshots[-1]["status"] == "streaming":
        engine.mark_interrupted("eof_without_end")
    return engine
