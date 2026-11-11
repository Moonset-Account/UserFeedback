"""只做 Range 读取的 HTTP 客户端。

职责：
- 通过 ``Range: bytes=`` 请求远程文件的指定区间，并核对响应状态与长度；
- 记录每一次实际请求的字节区间与字节数，作为"没有下载整个大包"的证据；
- 维护区间缓存：重叠/相邻区间只请求缺失部分，保证同一字节绝不重复下载；
- 捕获源站 ETag，每次响应都核对 ETag，发现源包变化立即报错；
- 提供 HEAD 元数据探测（总大小、是否支持 Range、ETag）。

刻意不做的事：不提供整包下载方法，不做磁盘缓存，不接受非 206 的区间响应。
"""

from __future__ import annotations

import dataclasses
import urllib.error
import urllib.request
from typing import Optional

from .errors import RangeNotSupported, RangeRequestError, SourceChanged


@dataclasses.dataclass
class RemoteMetadata:
    """HEAD 探测得到的远程文件元数据。"""

    url: str
    total_size: int
    etag: Optional[str]
    accept_ranges: bool
    status: int


@dataclasses.dataclass
class RangeRequestRecord:
    """一次 Range 请求的完整证据。"""

    index: int
    start: int
    end_inclusive: int
    requested_bytes: int
    response_status: int
    content_range: Optional[str]
    etag: Optional[str]
    bytes_returned: int

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class RangeClient:
    """基于 :mod:`urllib` 的最小 Range 客户端，带进程内区间缓存。"""

    def __init__(self, url: str, timeout: float = 30.0, user_agent: str = "selective-zip-collector/1.0"):
        self.url = url
        self.timeout = timeout
        self.user_agent = user_agent
        self._etag: Optional[str] = None
        self._records: list[RangeRequestRecord] = []
        self._total_size: Optional[int] = None
        # 已缓存的不相交、已排序闭区间 [(start, end, bytes)]
        self._chunks: list[tuple[int, int, bytes]] = []

    # ------------------------------------------------------------------
    # 证据与状态
    # ------------------------------------------------------------------
    @property
    def records(self) -> list[RangeRequestRecord]:
        return list(self._records)

    @property
    def total_downloaded_bytes(self) -> int:
        """实际从网络下载的字节（按每次响应计，缓存命中不计）。"""
        return sum(r.bytes_returned for r in self._records)

    @property
    def unique_cached_bytes(self) -> int:
        """当前缓存覆盖的不同字节总数。"""
        return sum(end - start + 1 for start, end, _ in self._chunks)

    @property
    def request_count(self) -> int:
        return len(self._records)

    @property
    def etag(self) -> Optional[str]:
        return self._etag

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def _open(self, req: urllib.request.Request):
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # HTTPError 同时也是 response 对象，交给上层按状态码处理
            return exc
        except urllib.error.URLError as exc:
            raise RangeRequestError(f"无法连接源站：{exc.reason}") from exc

    @staticmethod
    def _parse_etag(resp) -> Optional[str]:
        value = resp.headers.get("ETag")
        if value is None:
            return None
        return value.strip()

    def _check_etag(self, etag: Optional[str]) -> None:
        if etag is None:
            return
        if self._etag is None:
            self._etag = etag
        elif etag != self._etag:
            raise SourceChanged(
                "源包在采集中发生变化：响应 ETag 与首次请求不一致"
                f"（首次 {self._etag!r}，当前 {etag!r}）。本次结果全部作废。"
            )

    # ------------------------------------------------------------------
    # 公有 API
    # ------------------------------------------------------------------
    def head(self) -> RemoteMetadata:
        """探测远程文件：总大小、ETag、是否支持 Range。"""
        req = urllib.request.Request(self.url, method="HEAD", headers={"User-Agent": self.user_agent})
        resp = self._open(req)
        status = getattr(resp, "status", None) or resp.getcode()
        if status != 200:
            raise RangeRequestError(f"HEAD 请求失败，状态码 {status}")
        etag = self._parse_etag(resp)
        self._check_etag(etag)
        length = resp.headers.get("Content-Length")
        total_size = int(length) if length is not None else 0
        accept_ranges = (resp.headers.get("Accept-Ranges") or "").lower() == "bytes"
        self._total_size = total_size
        resp.close()
        return RemoteMetadata(
            url=self.url,
            total_size=total_size,
            etag=etag,
            accept_ranges=accept_ranges,
            status=status,
        )

    def get_range(self, start: int, end_inclusive: int) -> bytes:
        """读取闭区间 ``[start, end_inclusive]``，已缓存的字节不会重复请求。

        要求源站对未缓存部分返回 206 Partial Content；返回 200（整包）视为
        不支持 Range 并拒绝——保证采集器永远不会在无意中吞下整个大包。
        """
        if start < 0 or end_inclusive < start:
            raise RangeRequestError(f"非法字节区间 [{start}, {end_inclusive}]")
        missing = self._missing_segments(start, end_inclusive)
        for seg_start, seg_end in missing:
            self._fetch_and_store(seg_start, seg_end)
        return self._assemble(start, end_inclusive)

    # ------------------------------------------------------------------
    # 区间缓存
    # ------------------------------------------------------------------
    def _missing_segments(self, start: int, end: int) -> list[tuple[int, int]]:
        """计算 [start,end] 内尚未缓存的不相交子区间（已排序）。"""
        cursor = start
        missing: list[tuple[int, int]] = []
        for c_start, c_end, _ in self._chunks:
            if c_end < cursor:
                continue
            if c_start > end:
                break
            if c_start > cursor:
                missing.append((cursor, min(c_start - 1, end)))
            cursor = max(cursor, c_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            missing.append((cursor, end))
        return missing

    def _fetch_and_store(self, start: int, end_inclusive: int) -> None:
        headers = {
            "User-Agent": self.user_agent,
            "Range": f"bytes={start}-{end_inclusive}",
        }
        req = urllib.request.Request(self.url, headers=headers)
        resp = self._open(req)
        status = getattr(resp, "status", None) or resp.getcode()
        content_range = resp.headers.get("Content-Range")
        etag = self._parse_etag(resp)
        self._check_etag(etag)

        if status == 200:
            resp.close()
            raise RangeNotSupported(
                "源站忽略 Range 请求并返回了完整文件（200），"
                "无法保证选择性采集，任务中止。"
            )
        if status != 206:
            resp.close()
            raise RangeRequestError(f"Range 请求被拒绝，状态码 {status}，Content-Range={content_range!r}")

        data = resp.read()
        resp.close()
        expected = end_inclusive - start + 1
        self._records.append(
            RangeRequestRecord(
                index=len(self._records) + 1,
                start=start,
                end_inclusive=end_inclusive,
                requested_bytes=expected,
                response_status=status,
                content_range=content_range,
                etag=etag,
                bytes_returned=len(data),
            )
        )
        if len(data) != expected:
            raise RangeRequestError(
                f"区间 [{start}, {end_inclusive}] 期望 {expected} 字节，实际收到 {len(data)} 字节"
            )
        if content_range is not None:
            try:
                spec = content_range.split(" ", 1)[1]
                span, total = spec.split("/", 1)
                r_start, r_end = span.split("-", 1)
                if int(r_start) != start or int(r_end) != end_inclusive:
                    raise RangeRequestError(
                        f"Content-Range 与请求区间不符：请求 [{start},{end_inclusive}]，响应 {content_range!r}"
                    )
                if total != "*" and self._total_size is None:
                    self._total_size = int(total)
            except (IndexError, ValueError) as exc:
                raise RangeRequestError(f"无法解析 Content-Range：{content_range!r}") from exc
        self._store(start, data)

    def _store(self, start: int, data: bytes) -> None:
        """把新区间并入缓存（保持不相交、有序，合并相邻/重叠块）。"""
        new_end = start + len(data) - 1
        # 先把新区间与现有区间统一按起点排序，再线性合并
        ordered = sorted(self._chunks + [(start, new_end, data)], key=lambda item: item[0])
        merged: list[tuple[int, int, bytes]] = []
        for c_start, c_end, c_data in ordered:
            if not merged:
                merged.append([c_start, c_end, c_data])  # type: ignore[list-item]
                continue
            p_start, p_end, p_data = merged[-1]
            if c_start > p_end + 1:
                merged.append([c_start, c_end, c_data])  # type: ignore[list-item]
                continue
            # 相邻或重叠：以旧块为基准拼接。ETag 保证源不变，重叠字节内容必然一致。
            if c_end <= p_end:
                continue  # 新区间被旧块完全包含
            overlap = max(0, p_end - c_start + 1)
            merged[-1] = [p_start, c_end, p_data + c_data[overlap:]]
        self._chunks = [(a, b, blob) for a, b, blob in merged]

    def _assemble(self, start: int, end: int) -> bytes:
        parts: list[bytes] = []
        cursor = start
        for c_start, c_end, c_data in self._chunks:
            if c_end < cursor:
                continue
            if c_start > end:
                break
            seg = c_data[cursor - c_start : end - c_start + 1]
            parts.append(seg)
            cursor = c_end + 1
            if cursor > end:
                break
        result = b"".join(parts)
        if len(result) != end - start + 1:
            raise RangeRequestError(
                f"区间缓存不完整：请求 [{start},{end}]，仅能组装 {len(result)} 字节"
            )
        return result
