"""只读 IMAP 采集客户端。

数据通道：仅通过 IMAP 协议（标准库 imaplib）从本地模拟服务获取数据，
不直接读取生成器目录中的邮件文件或真值。所有写命令均不使用。
"""
from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass, field

from ..config import MAILBOX

_UID_RE = re.compile(rb"UID (\d+)")
_UIDVALIDITY_STATUS_RE = re.compile(rb"UIDVALIDITY (\d+)")


@dataclass
class FetchedMessage:
    uidvalidity: int
    uid: int
    raw: bytes
    flags: list[str] = field(default_factory=list)


@dataclass
class CollectResult:
    uidvalidity: int
    mailbox: str
    messages: list[FetchedMessage]
    protocol_log: list[str]


def _parse_uidvalidity_status(data) -> int | None:
    # imaplib status 返回形如 [b'INBOX (UIDVALIDITY 123 UIDNEXT 2 ...)']，
    # 不同版本可能嵌套 tuple，这里统一递归扫描 bytes 片段
    def _scan(value):
        if isinstance(value, (bytes, bytearray)):
            m = _UIDVALIDITY_STATUS_RE.search(bytes(value))
            if m:
                return int(m.group(1))
        elif isinstance(value, (list, tuple)):
            for sub in value:
                hit = _scan(sub)
                if hit is not None:
                    return hit
        return None

    return _scan(data)


def collect(host: str, port: int, username: str, password: str,
            *, mailbox: str = MAILBOX, timeout: float = 10.0) -> CollectResult:
    """连接模拟邮箱并以只读方式拉取全部邮件。"""
    protocol_log: list[str] = []
    client = imaplib.IMAP4(host, port, timeout=timeout)
    try:
        typ, _ = client.login(username, password)
        protocol_log.append(f"C: LOGIN <用户名> <密码已隐藏>  -> S: {typ}")
        if typ != "OK":
            raise RuntimeError(f"LOGIN 失败: {typ}")

        typ, data = client.status(mailbox, "(UIDVALIDITY UIDNEXT MESSAGES)")
        protocol_log.append(f"C: STATUS {mailbox} (UIDVALIDITY UIDNEXT MESSAGES) -> S: {typ} {data}")
        if typ != "OK":
            raise RuntimeError(f"STATUS 失败: {typ}")
        uidvalidity = _parse_uidvalidity_status(data)
        if uidvalidity is None:
            raise RuntimeError("STATUS 响应中缺少 UIDVALIDITY")

        # readonly=True 使 imaplib 发送 EXAMINE 而非 SELECT，确保不更改 \Seen 标志
        typ, select_data = client.select(mailbox, readonly=True)
        protocol_log.append(f"C: EXAMINE {mailbox} (READ-ONLY) -> S: {typ} {select_data}")
        if typ != "OK":
            raise RuntimeError(f"EXAMINE 失败: {typ}")

        typ, search_data = client.uid("SEARCH", None, "ALL")
        protocol_log.append(f"C: UID SEARCH ALL -> S: {typ}")
        if typ != "OK":
            raise RuntimeError(f"UID SEARCH 失败: {typ}")
        uid_tokens = search_data[0].split() if search_data and search_data[0] else []
        uids = [int(tok) for tok in uid_tokens]
        protocol_log.append(f"S: * SEARCH {' '.join(str(u) for u in uids)}")

        fetched: list[FetchedMessage] = []
        for uid in uids:
            typ, fetch_data = client.uid("FETCH", str(uid), "(UID FLAGS RFC822)")
            protocol_log.append(f"C: UID FETCH {uid} (UID FLAGS RFC822) -> S: {typ}, "
                                f"{len(fetch_data or [])} 个响应片段")
            if typ != "OK":
                raise RuntimeError(f"UID FETCH {uid} 失败: {typ}")
            raw = _extract_rfc822(fetch_data, uid)
            if raw is None:
                raise RuntimeError(f"UID FETCH {uid} 响应中未找到 RFC822 负载")
            flags = _extract_flags(fetch_data)
            fetched.append(FetchedMessage(uidvalidity=uidvalidity, uid=uid,
                                          raw=raw, flags=flags))

        typ, _ = client.logout()
        protocol_log.append(f"C: LOGOUT -> S: {typ}")
        return CollectResult(uidvalidity=uidvalidity, mailbox=mailbox,
                             messages=fetched, protocol_log=protocol_log)
    finally:
        try:
            client.socket and client.shutdown()
        except (OSError, Exception):  # noqa: BLE001 - 关闭阶段忽略一切异常
            pass


def _extract_rfc822(fetch_data, expected_uid: int) -> bytes | None:
    """从 imaplib 的 FETCH 响应结构中防御性提取 RFC822 字节。"""
    for fragment in fetch_data or []:
        if isinstance(fragment, tuple) and len(fragment) >= 2:
            header = fragment[0]
            if isinstance(header, bytes) and b"RFC822" in header:
                m = _UID_RE.search(header)
                if m and int(m.group(1)) != expected_uid:
                    continue
                payload = fragment[1]
                if isinstance(payload, bytes):
                    return payload
    # 兜底：任意带 RFC822 的 tuple
    for fragment in fetch_data or []:
        if isinstance(fragment, tuple) and len(fragment) >= 2 and isinstance(fragment[1], bytes):
            return fragment[1]
    return None


def _extract_flags(fetch_data) -> list[str]:
    for fragment in fetch_data or []:
        if isinstance(fragment, tuple) and fragment and isinstance(fragment[0], bytes):
            m = re.search(rb"FLAGS \(([^)]*)\)", fragment[0])
            if m:
                return m.group(1).decode("ascii").split()
        if isinstance(fragment, bytes):
            m = re.search(rb"FLAGS \(([^)]*)\)", fragment)
            if m:
                return m.group(1).decode("ascii").split()
    return []
