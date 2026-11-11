"""选择性采集编排：只取用户指定的成员。

采集顺序（每一步都是显式 Range 请求，证据由 :class:`RangeClient` 记录）：

1. HEAD 探测：总大小、ETag、是否支持 Range；不支持 Range 直接拒绝；
2. 读取文件尾部窗口，定位 EOCD；ZIP64 时沿 Locator/Record 定位中央目录；
3. 一次性读取中央目录区间，解析全部条目并建立成员索引；
4. 包级子集检查：加密、分卷一律拒绝；选中成员的压缩方式/版本逐一检查；
5. 对每个选中成员：读取本地文件头 → 与中央目录逐项核对；
6. 按中央目录声明的压缩大小读取压缩数据（bit3 再单独读数据描述符核对）；
7. 解压（仅 stored/deflate）并核对 CRC-32 与未压缩大小；
8. 记录级校验（对象版本、完整性），不通过的成员拒绝入册；
9. 全程 ETag 指纹校验，源包一旦变化立即中止，已得结果全部作废。
"""

from __future__ import annotations

import dataclasses
import time
import zlib
from typing import Any, Callable, Optional

from . import protocol as P
from . import zip_structs
from .errors import (
    CrcMismatch,
    EncryptedArchive,
    MemberNotFound,
    MultiVolumeArchive,
    RangeNotSupported,
    SelectiveFetchError,
    SizeMismatch,
    UnsupportedCompression,
    UnsupportedVersion,
)
from .range_client import RangeClient
from .records import ParsedMember, RejectedMember, parse_and_validate_member
from .zip_structs import CentralEntry, EocdInfo


@dataclasses.dataclass
class MemberFetchEvidence:
    """单个成员的采集证据。"""

    member_name: str
    local_header_offset: int
    data_offset: int
    compressed_size: int
    uncompressed_size: int
    compression: str
    crc32_declared: int
    crc32_actual: int
    byte_ranges: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _compression_name(value: int) -> str:
    return {P.COMPRESSION_STORED: "stored(0)", P.COMPRESSION_DEFLATE: "deflate(8)"}.get(
        value, f"unknown({value})"
    )


class SelectiveFetcher:
    """对单个远程 ZIP 执行一次选择性采集。"""

    def __init__(
        self,
        url: str,
        wanted_members: list[str],
        experiment_id: str,
        allowed_versions: tuple[int, ...] = (1, 2, 3),
        timeout: float = 30.0,
        progress: Optional[Callable[[str], None]] = None,
    ):
        if not wanted_members:
            raise SelectiveFetchError("至少需要指定一个成员名")
        self.url = url
        self.wanted_members = list(dict.fromkeys(wanted_members))  # 去重保序
        self.experiment_id = experiment_id
        self.allowed_versions = allowed_versions
        self.timeout = timeout
        self._progress = progress or (lambda _msg: None)
        self.client = RangeClient(url, timeout=timeout)
        self._total_size = 0
        self._metadata = None
        self._eocd: Optional[EocdInfo] = None

    def _log(self, message: str) -> None:
        self._progress(message)

    def _traffic_block(self) -> dict[str, Any]:
        total = self._metadata.total_size if self._metadata else 0
        downloaded = self.client.total_downloaded_bytes
        return {
            "request_count": self.client.request_count,
            "downloaded_bytes": downloaded,
            "unique_bytes_touched": self.client.unique_cached_bytes,
            "remote_total_bytes": total,
            "downloaded_ratio": round(downloaded / total, 4) if total else None,
            "saved_bytes": total - downloaded if total else None,
        }

    def _remote_block(self) -> dict[str, Any]:
        return {
            "total_size": self._metadata.total_size if self._metadata else 0,
            "etag": self.client.etag,
            "is_zip64": self._eocd.is_zip64 if self._eocd else None,
            "total_entries": self._eocd.total_entries if self._eocd else None,
        }

    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self._metadata = self._probe()
            self._eocd = self._locate_central_directory(self._metadata.total_size)
            entries = self._read_central_directory(self._eocd)
            self._check_package_subset(entries)
            selected = self._select_entries(entries)
            accepted, rejected, member_evidence = self._fetch_members(selected)
        except SelectiveFetchError as exc:
            return self._failure_report(exc, started)

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        records = [m.to_record() for m in accepted]
        return {
            "status": "completed_with_rejections" if rejected else "success",
            "url": self.url,
            "experiment_id": self.experiment_id,
            "error": None,
            "protocol_subset": P.SUPPORTED_SUBSET_DESCRIPTION,
            "remote": self._remote_block(),
            "requested_members": self.wanted_members,
            "records": records,
            "rejected_members": [r.to_dict() for r in rejected],
            "member_evidence": [e.to_dict() for e in member_evidence],
            "range_requests": [r.to_dict() for r in self.client.records],
            "traffic": self._traffic_block(),
            "elapsed_ms": elapsed_ms,
        }

    def _failure_report(self, exc: SelectiveFetchError, started: float) -> dict[str, Any]:
        return {
            "status": "failed",
            "url": self.url,
            "experiment_id": self.experiment_id,
            "error": {"code": exc.code, "message": str(exc)},
            "protocol_subset": P.SUPPORTED_SUBSET_DESCRIPTION,
            "remote": self._remote_block(),
            "requested_members": self.wanted_members,
            "records": [],
            "rejected_members": [],
            "member_evidence": [],
            "range_requests": [r.to_dict() for r in self.client.records],
            "traffic": self._traffic_block(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # ------------------------------------------------------------------
    # 步骤 1：HEAD 探测
    # ------------------------------------------------------------------
    def _probe(self):
        self._log("HEAD 探测源站元数据……")
        metadata = self.client.head()
        if metadata.total_size <= 0:
            raise RangeNotSupported("源站未给出 Content-Length，无法计算字节区间")
        if not metadata.accept_ranges:
            raise RangeNotSupported(
                "源站未声明 Accept-Ranges: bytes，选择性采集没有协议保障，任务中止"
            )
        self._total_size = metadata.total_size
        self._log(f"文件总大小 {metadata.total_size} 字节，ETag={metadata.etag!r}")
        return metadata

    # ------------------------------------------------------------------
    # 步骤 2：定位 EOCD 与中央目录
    # ------------------------------------------------------------------
    def _locate_central_directory(self, total_size: int) -> EocdInfo:
        self._log("通过 Range 读取文件尾部以定位 EOCD……")
        # 先用小窗口试探，确保小包也不会被一次性整包下载；找不到再扩大。
        # 64 字节足以覆盖普通 ZIP 的 EOCD(22)+短注释；
        # 256 字节覆盖 ZIP64 的 Record(56)+Locator(20)+EOCD(22)。
        windows = [64, 256, 1024, 4096, P.EOCD_SEARCH_WINDOW]
        tail: bytes | None = None
        eocd: EocdInfo | None = None
        last_error: Exception | None = None
        for window in windows:
            window = min(window, total_size)
            tail = self.client.get_range(total_size - window, total_size - 1)
            try:
                eocd, _eocd_offset, _tail_start = zip_structs.find_eocd(tail, total_size)
                break
            except zip_structs.ZipStructureError as exc:
                last_error = exc
                # 显式"窗口不够"类错误才扩大窗口；签名缺失也可能是窗口太小
                continue
        if eocd is None:
            assert last_error is not None
            raise last_error

        # 若 ZIP64 Record 不在窗口内（理论上罕见），扩大到覆盖整个 Record
        if eocd.is_zip64:
            assert eocd.zip64_eocd_offset is not None
            record_end = eocd.zip64_eocd_offset + P.SIZE_ZIP64_EOCD_RECORD_FIXED - 1
            if record_end > total_size - 1:
                raise zip_structs.ZipStructureError("ZIP64 EOCD Record 末端超出文件边界")
        if eocd.disk_number != 0 or eocd.cd_disk_number != 0:
            raise MultiVolumeArchive(
                f"EOCD 显示该归档跨越多卷（当前盘={eocd.disk_number}，目录盘={eocd.cd_disk_number}），"
                "本项目明确不支持分卷 ZIP"
            )
        self._log(
            f"中央目录位于偏移 {eocd.central_dir_offset}，大小 {eocd.central_dir_size} 字节"
            f"（{'ZIP64' if eocd.is_zip64 else 'ZIP'}，共 {eocd.total_entries} 个成员）"
        )
        return eocd

    # ------------------------------------------------------------------
    # 步骤 3：读取并解析中央目录
    # ------------------------------------------------------------------
    def _read_central_directory(self, eocd: EocdInfo) -> list[CentralEntry]:
        start = eocd.central_dir_offset
        end = start + eocd.central_dir_size - 1
        self._log(f"Range 读取中央目录区间 [{start}, {end}]……")
        cd_bytes = self.client.get_range(start, end)
        return zip_structs.parse_central_directory(cd_bytes, eocd.total_entries)

    # ------------------------------------------------------------------
    # 步骤 4：包级子集检查
    # ------------------------------------------------------------------
    def _check_package_subset(self, entries: list[CentralEntry]) -> None:
        encrypted = [e.filename for e in entries if e.is_encrypted]
        if encrypted:
            raise EncryptedArchive(
                "检测到加密成员，本项目明确拒绝加密 ZIP：" + "、".join(encrypted[:5])
            )
        for entry in entries:
            if entry.disk_number_start != 0:
                raise MultiVolumeArchive(
                    f"成员 {entry.filename!r} 起始盘号为 {entry.disk_number_start}，属于分卷归档"
                )

    def _select_entries(self, entries: list[CentralEntry]) -> list[CentralEntry]:
        index = {e.filename: e for e in entries}
        missing = [name for name in self.wanted_members if name not in index]
        if missing:
            raise MemberNotFound(
                "以下成员在中央目录中不存在：" + "、".join(missing)
                + f"。目录内可用成员示例：{', '.join(list(index)[:8])}"
            )
        selected: list[CentralEntry] = []
        for name in self.wanted_members:
            entry = index[name]
            if entry.compression not in P.SUPPORTED_COMPRESSIONS:
                raise UnsupportedCompression(
                    f"成员 {name!r} 使用压缩方式 {_compression_name(entry.compression)}，"
                    f"不在支持子集 {sorted(P.SUPPORTED_COMPRESSIONS)} 内"
                )
            if entry.version_needed > P.MAX_EXTRACT_VERSION:
                raise UnsupportedVersion(
                    f"成员 {name!r} 要求提取版本 {entry.version_needed / 10:.1f}，"
                    f"高于支持上限 {P.MAX_EXTRACT_VERSION / 10:.1f}"
                )
            selected.append(entry)
        return selected

    # ------------------------------------------------------------------
    # 步骤 5-8：逐成员采集
    # ------------------------------------------------------------------
    def _fetch_members(self, selected: list[CentralEntry]):
        accepted: list[ParsedMember] = []
        rejected: list[RejectedMember] = []
        evidence: list[MemberFetchEvidence] = []
        # 记录每个成员采集开始时的请求序号，便于证据中按成员切分 Range 记录
        for entry in selected:
            mark = self.client.request_count
            try:
                payload, crc_actual, data_offset = self._fetch_one_member(entry)
                parsed = parse_and_validate_member(
                    entry.filename, payload, self.experiment_id, self.allowed_versions
                )
            except SelectiveFetchError as exc:
                # 记录级拒绝（MemberRejected）保留任务，其他错误向上抛为整任务失败
                from .errors import MemberRejected as _MR

                if isinstance(exc, _MR):
                    rejected.append(RejectedMember(member_name=entry.filename, reason=str(exc)))
                    self._log(f"成员 {entry.filename!r} 被拒绝：{exc}")
                    continue
                raise
            member_ranges = [r.to_dict() for r in self.client.records[mark:]]
            evidence.append(
                MemberFetchEvidence(
                    member_name=entry.filename,
                    local_header_offset=entry.local_header_offset,
                    data_offset=data_offset,
                    compressed_size=entry.compressed_size,
                    uncompressed_size=entry.uncompressed_size,
                    compression=_compression_name(entry.compression),
                    crc32_declared=entry.crc32,
                    crc32_actual=crc_actual,
                    byte_ranges=member_ranges,
                )
            )
            accepted.append(parsed)
            self._log(
                f"成员 {entry.filename!r} 采集成功：{entry.compressed_size} 压缩字节 → "
                f"{entry.uncompressed_size} 字节，CRC 核对通过"
            )
        return accepted, rejected, evidence

    def _fetch_one_member(self, entry: CentralEntry) -> tuple[bytes, int, int]:
        self._log(f"读取成员 {entry.filename!r} 的本地文件头……")
        # 小探针：绝大多数本地头（30 固定 + 短文件名/扩展）都在 128 字节内；
        # RangeClient 有区间缓存，补读时已缓存部分不会重复请求。
        probe_size = 128
        probe_end = min(entry.local_header_offset + probe_size - 1, self._total_size - 1)
        block = self.client.get_range(entry.local_header_offset, probe_end)
        try:
            header = zip_structs.parse_local_header(block)
        except zip_structs.ZipStructureError:
            bigger_end = min(
                entry.local_header_offset + P.LOCAL_HEADER_PROBE - 1,
                self._total_size - 1,
            )
            if bigger_end <= probe_end:
                raise
            block = self.client.get_range(entry.local_header_offset, bigger_end)
            header = zip_structs.parse_local_header(block)

        zip_structs.verify_header_consistency(entry, header)
        data_offset = entry.local_header_offset + header.header_total_size
        data_end = data_offset + entry.compressed_size - 1
        self._log(
            f"Range 读取成员压缩数据 [{data_offset}, {data_end}]"
            f"（{entry.compressed_size} 字节）……"
        )
        compressed = self.client.get_range(data_offset, data_end)

        if entry.has_data_descriptor:
            # 描述符可能带签名（16 字节）或不带签名（12 字节）。
            # 先取 16 字节窗口，解析器按首 4 字节是否为签名自适应；
            # 借助 RangeClient 缓存，多读的 4 字节若与下一个成员相邻也不会重复请求。
            desc_window_end = min(data_end + 16, self._total_size - 1)
            desc_block = self.client.get_range(data_end + 1, desc_window_end)
            zip_structs.parse_data_descriptor(desc_block, entry)

        if entry.compression == P.COMPRESSION_STORED:
            payload = compressed
        else:
            # -15 表示原始 DEFLATE（zlib 包装之外的格式，ZIP 使用该形式）
            try:
                payload = zlib.decompress(compressed, -15)
            except zlib.error as exc:
                # 压缩流被破坏（如验收场景中的字节翻转）：无法得到可信数据
                raise CrcMismatch(
                    f"成员 {entry.filename!r} 的 DEFLATE 压缩流已损坏，解压失败：{exc}。"
                    "数据不可信，拒绝入册。"
                ) from exc

        if len(payload) != entry.uncompressed_size:
            raise SizeMismatch(
                f"成员 {entry.filename!r} 解压后 {len(payload)} 字节，"
                f"与声明 {entry.uncompressed_size} 字节不符"
            )
        crc_actual = zlib.crc32(payload) & 0xFFFFFFFF
        if crc_actual != entry.crc32:
            raise CrcMismatch(
                f"成员 {entry.filename!r} CRC-32 校验失败："
                f"实际 0x{crc_actual:08x}，中央目录声明 0x{entry.crc32:08x}"
            )
        return payload, crc_actual, data_offset


def run_selective_fetch(
    url: str,
    wanted_members: list[str],
    experiment_id: str,
    allowed_versions: tuple[int, ...] = (1, 2, 3),
    timeout: float = 30.0,
    progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """便捷函数：执行一次选择性采集并返回可 JSON 序列化的报告。"""
    return SelectiveFetcher(
        url=url,
        wanted_members=wanted_members,
        experiment_id=experiment_id,
        allowed_versions=allowed_versions,
        timeout=timeout,
        progress=progress,
    ).run()
