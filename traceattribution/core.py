"""培训素材分发副本的分散标记写入与归属核查。

标记写入文本素材允许保留的元数据位置（行注释位），绑定批次与副本编号，
并通过 HMAC 绑定正文内容以验证完整性；核查阶段对随意排序的文件集合
去重后统计不同位置数，按配置阈值判定 确认/冲突/证据不足。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Union

__all__ = [
    "MARKER_TAG",
    "TraceConfig",
    "Marker",
    "ScanResult",
    "Status",
    "Attribution",
    "protect_text",
    "protect_pack",
    "extract_body",
    "scan_file",
    "attribute",
]

MARKER_TAG = "@trace1"

#: 允许承载标记的行注释前缀（可编辑文本素材通常保留这些元数据位置）。
COMMENT_PREFIXES = ("#", "//", ";", "--")

_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_MARKER_RE = re.compile(
    r"^\s*(?:(?:#|//|;|--)\s*)?@trace1"
    r" batch=(?P<batch>\S+)"
    r" copy=(?P<copy>\S+)"
    r" pos=(?P<pos>\d+)"
    r" mac=(?P<mac>[0-9a-f]{64})"
    r"\s*(?:-->)?\s*$"
)

#: 任何形似标记保留位的行（含解析失败的），都不属于正文。
_LOOKALIKE_RE = re.compile(r"^\s*(?:(?:#|//|;|--)\s*)?@trace1(?![\w-])")


class TraceConfig:
    """标记与核查配置。

    key:        HMAC 密钥（完整性校验共享密钥）。
    positions:  分散标记的位置槽总数 K。
    threshold:  确认归属所需的不同位置数（1 <= threshold <= positions）。
    comment_prefix: 写入标记时使用的行注释前缀。
    """

    def __init__(
        self,
        key: bytes,
        positions: int = 4,
        threshold: int = 3,
        comment_prefix: str = "#",
    ) -> None:
        if not isinstance(key, (bytes, bytearray)) or not key:
            raise ValueError("key 必须为非空字节串")
        if positions < 1:
            raise ValueError("positions 必须 >= 1")
        if not 1 <= threshold <= positions:
            raise ValueError("threshold 必须满足 1 <= threshold <= positions")
        if comment_prefix not in COMMENT_PREFIXES:
            raise ValueError(f"comment_prefix 必须是 {COMMENT_PREFIXES} 之一")
        self.key = bytes(key)
        self.positions = positions
        self.threshold = threshold
        self.comment_prefix = comment_prefix

    def __repr__(self) -> str:  # 不泄露密钥
        return (
            f"TraceConfig(key=<redacted>, positions={self.positions}, "
            f"threshold={self.threshold}, comment_prefix={self.comment_prefix!r})"
        )


@dataclass(frozen=True)
class Marker:
    batch: str
    copy_no: str
    pos: int
    mac: str


@dataclass(frozen=True)
class ScanResult:
    markers: tuple[Marker, ...]
    malformed: int  # 形似标记但格式非法的行数


class Status(Enum):
    CONFIRMED = "confirmed"        # 确认归属
    CONFLICT = "conflict"          # 混有两个及以上副本
    INSUFFICIENT = "insufficient"  # 证据不足


@dataclass(frozen=True)
class Attribution:
    status: Status
    batch: Union[str, None] = None
    copy_no: Union[str, None] = None
    positions: frozenset = frozenset()          # 归属身份覆盖到的不同位置
    identities: frozenset = frozenset()         # 全部有效标记指向的 (batch, copy)
    files_seen: int = 0                          # 输入文件项总数（含重复）
    files_unique: int = 0                        # 去重后的文件数
    valid_markers: int = 0
    invalid_markers: int = 0
    detail: str = ""


def _validate_ident(value: str, field_name: str) -> None:
    if not _IDENT_RE.match(value):
        raise ValueError(f"{field_name} 仅允许 [A-Za-z0-9._-]，长度 1..64: {value!r}")


def extract_body(text: str) -> str:
    """移除所有标记保留位行，返回正文。正文内容保持与写入前一致。"""
    return "".join(
        line for line in text.splitlines(keepends=True) if not _LOOKALIKE_RE.match(line)
    )


def _body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _mac(config: TraceConfig, batch: str, copy_no: str, pos: int, body: str) -> str:
    msg = f"trace1|{batch}|{copy_no}|{pos}|{_body_digest(body)}".encode("utf-8")
    return hmac.new(config.key, msg, hashlib.sha256).hexdigest()


def _format_marker(config: TraceConfig, marker: Marker) -> str:
    return (
        f"{config.comment_prefix} {MARKER_TAG} batch={marker.batch}"
        f" copy={marker.copy_no} pos={marker.pos} mac={marker.mac}\n"
    )


def protect_text(
    text: str,
    batch: str,
    copy_no: str,
    slots: Iterable[int],
    config: TraceConfig,
) -> str:
    """在文本的元数据位置写入指定位置槽的分散标记，返回新文本。

    正文保持不变（extract_body 结果与输入一致）；同一输入输出确定。
    正文若已包含形似标记保留位的行则拒绝写入，避免归属歧义。
    """
    _validate_ident(batch, "batch")
    _validate_ident(copy_no, "copy_no")
    slot_list = sorted(set(slots))
    for s in slot_list:
        if not 0 <= s < config.positions:
            raise ValueError(f"位置槽越界: {s}（允许 0..{config.positions - 1}）")
    for line in text.splitlines():
        if _LOOKALIKE_RE.match(line):
            raise ValueError("正文已包含标记保留位行，拒绝写入以避免归属歧义")

    body = text
    lines = text.splitlines(keepends=True)
    n = len(lines)

    def insert_index(slot: int) -> int:
        if config.positions == 1:
            return n
        return round(slot * n / (config.positions - 1))

    by_index: dict[int, list[str]] = {}
    for s in slot_list:
        marker = Marker(batch, copy_no, s, _mac(config, batch, copy_no, s, body))
        by_index.setdefault(insert_index(s), []).append(_format_marker(config, marker))

    if n in by_index and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    out: list[str] = []
    for i, line in enumerate(lines):
        out.extend(by_index.get(i, ()))
        out.append(line)
    out.extend(by_index.get(n, ()))
    return "".join(out)


def protect_pack(
    files: Mapping[str, str],
    batch: str,
    copy_no: str,
    config: TraceConfig,
) -> dict[str, str]:
    """为一个分发副本的整包文件写入分散标记。

    位置槽按文件顺序轮转分配；返回 {文件名: 写入标记后的文本}。
    """
    names = list(files)
    if not names:
        raise ValueError("文件集合不能为空")
    assignment: dict[str, list[int]] = {name: [] for name in names}
    for slot in range(config.positions):
        assignment[names[slot % len(names)]].append(slot)
    return {
        name: protect_text(files[name], batch, copy_no, assignment[name], config)
        for name in names
    }


def scan_file(text: str) -> ScanResult:
    """解析单个文件文本中的标记（仅格式解析，不做完整性校验）。"""
    markers: list[Marker] = []
    malformed = 0
    for line in text.splitlines():
        m = _MARKER_RE.match(line)
        if m:
            markers.append(
                Marker(m.group("batch"), m.group("copy"), int(m.group("pos")), m.group("mac"))
            )
        elif _LOOKALIKE_RE.match(line):
            malformed += 1
    return ScanResult(tuple(markers), malformed)


def attribute(
    files: Union[Mapping[str, str], Iterable[tuple]],
    config: TraceConfig,
) -> Attribution:
    """对随意排序的文件集合进行归属核查。

    规则：
    1. 重复文件（内容相同）只计一次；
    2. 标记须通过完整性校验（HMAC 绑定批次/副本/位置/正文）方为有效；
    3. 有效标记指向两个及以上不同 (批次, 副本) -> CONFLICT；
    4. 唯一身份且不同位置数 >= config.threshold -> CONFIRMED；
    5. 其余情况 -> INSUFFICIENT。
    """
    items = list(files.items()) if isinstance(files, Mapping) else list(files)
    seen_hashes: set[str] = set()
    valid: dict[tuple[str, str], set[int]] = {}
    valid_count = 0
    invalid_count = 0
    files_unique = 0

    for _name, text in items:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        files_unique += 1
        body = extract_body(text)
        for line in text.splitlines():
            m = _MARKER_RE.match(line)
            if not m:
                if _LOOKALIKE_RE.match(line):
                    invalid_count += 1
                continue
            batch, copy_no, pos_s, mac = (
                m.group("batch"),
                m.group("copy"),
                m.group("pos"),
                m.group("mac"),
            )
            pos = int(pos_s)
            if (
                pos >= config.positions
                or not _IDENT_RE.match(batch)
                or not _IDENT_RE.match(copy_no)
            ):
                invalid_count += 1
                continue
            expected = _mac(config, batch, copy_no, pos, body)
            if not hmac.compare_digest(expected, mac):
                invalid_count += 1
                continue
            valid.setdefault((batch, copy_no), set()).add(pos)
            valid_count += 1

    identities = frozenset(valid)
    base = dict(
        files_seen=len(items),
        files_unique=files_unique,
        valid_markers=valid_count,
        invalid_markers=invalid_count,
        identities=identities,
    )

    if len(valid) >= 2:
        return Attribution(
            status=Status.CONFLICT,
            detail=f"有效标记混有 {len(valid)} 个不同副本: {sorted(identities)}",
            **base,
        )
    if len(valid) == 1:
        (ident, positions), = valid.items()
        batch, copy_no = ident
        frozen = frozenset(positions)
        if len(positions) >= config.threshold:
            return Attribution(
                status=Status.CONFIRMED,
                batch=batch,
                copy_no=copy_no,
                positions=frozen,
                detail=(
                    f"归属确认: 批次 {batch} 副本 {copy_no}，"
                    f"覆盖 {len(positions)} 个不同位置（阈值 {config.threshold}）"
                ),
                **base,
            )
        return Attribution(
            status=Status.INSUFFICIENT,
            batch=batch,
            copy_no=copy_no,
            positions=frozen,
            detail=(
                f"证据不足: 仅覆盖 {len(positions)} 个不同位置，"
                f"未达到阈值 {config.threshold}"
            ),
            **base,
        )
    return Attribution(
        status=Status.INSUFFICIENT,
        detail="证据不足: 未找到任何有效标记",
        **base,
    )
