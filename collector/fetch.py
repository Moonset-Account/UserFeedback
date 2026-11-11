"""采集层：只通过公开接口取得 ICS 文本。

允许的来源（规定的公开接口/文件）：
- http://、https:// URL：使用标准库 urllib 发起普通 GET；
- file:// URL 或本地文件路径：从磁盘读取。

本模块不做任何解析，也不读取测试真值目录。返回的 SourceEvidence
记录了来源、字节数与获取方式，作为网页上"来源证据"的一部分。
"""

from __future__ import annotations

import dataclasses
import pathlib
import urllib.request
from urllib.parse import urlparse

from .errors import Issue, IssueCode

HTTP_TIMEOUT_SECONDS = 15
MAX_BYTES = 5 * 1024 * 1024  # 5 MiB，防止异常源站拖垮采集任务
USER_AGENT = "ObservationWindowCollector/1.0 (+local demo; icalendar acquisition only)"


@dataclasses.dataclass
class SourceEvidence:
    source: str
    kind: str               # "http" | "file"
    byte_count: int
    content_type: str | None
    fetched_ok: bool

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class FetchResult:
    text: str | None
    evidence: SourceEvidence
    issue: Issue | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None and self.issue is None


def _read_http(source: str) -> FetchResult:
    request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_BYTES + 1)
            content_type = resp.headers.get("Content-Type")
    except Exception as exc:  # urllib 的异常种类较多，统一结构化上报
        issue = Issue.make(
            IssueCode.FETCH_ERROR,
            f"HTTP 获取失败：{type(exc).__name__}: {exc}",
        )
        return FetchResult(None, SourceEvidence(source, "http", 0, None, False), issue)

    if len(raw) > MAX_BYTES:
        issue = Issue.make(
            IssueCode.FETCH_ERROR,
            f"源数据超过 {MAX_BYTES} 字节上限，已中止采集",
        )
        return FetchResult(None, SourceEvidence(source, "http", len(raw), content_type, False), issue)

    # ICS 实际编码由解析层处理；这里按 UTF-8（RFC 5545 默认）读取，失败再回退 latin-1
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return FetchResult(
        text,
        SourceEvidence(source, "http", len(raw), content_type, True),
    )


def _read_file(source: str, path: pathlib.Path) -> FetchResult:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        issue = Issue.make(
            IssueCode.FETCH_ERROR,
            f"文件读取失败：{type(exc).__name__}: {exc}",
        )
        return FetchResult(None, SourceEvidence(source, "file", 0, None, False), issue)

    if len(raw) > MAX_BYTES:
        issue = Issue.make(
            IssueCode.FETCH_ERROR,
            f"源数据超过 {MAX_BYTES} 字节上限，已中止采集",
        )
        return FetchResult(None, SourceEvidence(source, "file", len(raw), None, False), issue)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return FetchResult(
        text,
        SourceEvidence(source, "file", len(raw), "text/calendar", True),
    )


def fetch(source: str) -> FetchResult:
    """从规定来源获取 ICS 文本。source 可以是 URL 或本地路径。"""
    parsed = urlparse(source)
    scheme = parsed.scheme.lower()

    if scheme in ("http", "https"):
        return _read_http(source)

    if scheme == "file":
        return _read_file(source, pathlib.Path(parsed.path))

    if scheme == "":
        # 没有 scheme 时按本地文件路径处理
        return _read_file(source, pathlib.Path(source))

    issue = Issue.make(
        IssueCode.UNSUPPORTED_SCHEME,
        f"不支持的来源协议：{scheme or '(空)'}，仅允许 http(s) 与本地文件",
    )
    return FetchResult(None, SourceEvidence(source, scheme or "unknown", 0, None, False), issue)
