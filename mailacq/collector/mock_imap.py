"""本地最小 IMAP4rev1 模拟服务（纯标准库，不连接任何真实邮箱）。

支持的协议子集（与 README 声明一致）：
- 传输：TCP + 行协议，支持请求中的 {n} literal 与响应中的 {n} literal；
- 未认证：CAPABILITY、LOGIN（任意虚构凭据）、LOGOUT；
- 已认证：NOOP、SELECT/EXAMINE（始终 READ-ONLY）、STATUS (UIDVALIDITY UIDNEXT)、
  UID SEARCH ALL、UID FETCH <set> (UID FLAGS RFC822)、LOGOUT；
- 写命令（STORE/APPEND/DELETE/EXPUNGE 等）一律返回 NO，模拟只读邮箱。

服务输入只来自任务编排层传入的邮件字节（生成器产物经 runner 载入），
不读取测试专用的独立真值文件。
"""
from __future__ import annotations

import socketserver
import threading
from dataclasses import dataclass, field

CRLF = b"\r\n"
CAPABILITY = (
    "IMAP4rev1 AUTH=PLAIN "
    "UIDPLUS"  # 仅声明能力名，未实现 UIDPLUS 扩展命令
)
# 只读命令白名单（未认证阶段另有限制）
_MUTATING = {"STORE", "APPEND", "DELETE", "EXPUNGE", "COPY", "MOVE", "CREATE",
             "DELETE", "RENAME", "SUBSCRIBE", "UNSUBSCRIBE"}


@dataclass
class MailboxState:
    uidvalidity: int
    messages: list[tuple[int, bytes]]  # [(uid, rfc822_bytes)]
    flags: dict[int, list[str]] = field(default_factory=dict)


class _IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: MockIMAPServer = self.server  # type: ignore[assignment]
        server.register_client(self)
        self._authenticated = False
        self._selected = False
        self._send_line("* OK [CAPABILITY " + CAPABILITY + "] "
                        "mock-imap 本地虚构邮箱已就绪")
        try:
            while True:
                line = self._read_command_line()
                if line is None:
                    break
                if not self._dispatch(line, server):
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            server.unregister_client(self)

    # ---------- 底层读写 ----------
    def _read_command_line(self) -> str | None:
        buf = bytearray()
        while True:
            ch = self.rfile.read(1)
            if not ch:
                return None
            buf += ch
            if buf.endswith(CRLF):
                line = bytes(buf[:-2]).decode("utf-8", errors="replace")
                # 处理请求 literal：行尾形如 "TAG APPEND INBOX {12}\r\n"
                stripped = line.rstrip()
                if stripped.endswith("}") and "{" in stripped:
                    literal_spec = stripped[stripped.rfind("{") + 1:-1]
                    if literal_spec.isdigit():
                        size = int(literal_spec)
                        # 先发 + 续请并读完 literal 字节，保持协议同步
                        self._send_line("+ Ready for literal data")
                        self.rfile.read(size)
                        rest = self._read_optional_line()
                        return stripped + (" " + rest if rest else "")
                return line

    def _read_optional_line(self) -> str | None:
        """literal 之后若有剩余参数则读取一行，否则返回 None。"""
        self.connection.settimeout(0.5)
        try:
            rest = self.rfile.readline()
            if not rest:
                return None
            return rest[:-2].decode("utf-8", errors="replace") if rest.endswith(CRLF) \
                else rest.decode("utf-8", errors="replace")
        except (TimeoutError, OSError):
            return None
        finally:
            self.connection.settimeout(None)

    def _send_line(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8") + CRLF)
        self.wfile.flush()

    def _send_literal(self, data: bytes) -> None:
        """发送非同步 literal 头 {n}，调用方随后写入恰好 n 个字节。

        imaplib 在响应行中读到 {n} 后会精确读取 n 字节，
        因此这里不能插入 + 续请（续请仅用于客户端 -> 服务器的请求 literal）。
        """
        self.wfile.write(b"{" + str(len(data)).encode("ascii") + b"}" + CRLF)
        self.wfile.flush()

    def _tagged(self, tag: str, status: str, text: str) -> None:
        self._send_line(f"{tag} {status} {text}")

    # ---------- 命令分发 ----------
    def _dispatch(self, line: str, server: "MockIMAPServer") -> bool:
        parts = line.split(" ", 2)
        if len(parts) < 2:
            return True
        tag, command = parts[0], parts[1].upper()
        rest = parts[2] if len(parts) > 2 else ""
        server.log(f"{tag} {command} {rest}".strip())

        if command == "CAPABILITY":
            self._send_line("* CAPABILITY " + CAPABILITY)
            self._tagged(tag, "OK", "CAPABILITY completed")
        elif command == "LOGIN":
            self._authenticated = True
            self._tagged(tag, "OK", "LOGIN completed (虚构凭据，仅本地模拟)")
        elif command == "LOGOUT":
            self._send_line("* BYE mock-imap 正在关闭连接")
            self._tagged(tag, "OK", "LOGOUT completed")
            return False
        elif not self._authenticated:
            self._tagged(tag, "BAD", "请先 LOGIN")
        elif command in _MUTATING:
            self._tagged(tag, "NO", "模拟邮箱为只读，拒绝写命令 " + command)
        elif command in ("SELECT", "EXAMINE"):
            self._select(tag, server, read_only=True)
        elif command == "STATUS":
            self._status(tag, rest, server)
        elif command == "UID":
            self._uid(tag, rest, server)
        elif command in ("NOOP",):
            self._tagged(tag, "OK", "NOOP completed")
        elif command in ("CLOSE", "UNSELECT"):
            self._selected = False
            self._tagged(tag, "OK", f"{command} completed")
        else:
            self._tagged(tag, "BAD", f"协议子集不支持的命令: {command}")
        return True

    def _select(self, tag: str, server: "MockIMAPServer", *, read_only: bool) -> None:
        state = server.state
        n = len(state.messages)
        self._send_line(f"* {n} EXISTS")
        self._send_line("* 0 RECENT")
        self._send_line("* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
        self._send_line(f"* OK [UIDVALIDITY {state.uidvalidity}] UID 代际")
        next_uid = max((uid for uid, _ in state.messages), default=0) + 1
        self._send_line(f"* OK [UIDNEXT {next_uid}] 下一个 UID")
        self._send_line("* OK [PERMANENTFLAGS ()] 只读邮箱无永久标志")
        self._selected = True
        mode = "READ-ONLY" if read_only else "READ-WRITE"
        self._tagged(tag, "OK", f"[{mode}] EXAMINE/SELECT completed")

    def _status(self, tag: str, rest: str, server: "MockIMAPServer") -> None:
        # STATUS INBOX (UIDVALIDITY UIDNEXT UIDNEXT ...)
        state = server.state
        next_uid = max((uid for uid, _ in state.messages), default=0) + 1
        self._send_line(
            f"* STATUS INBOX (UIDVALIDITY {state.uidvalidity} UIDNEXT {next_uid} "
            f"MESSAGES {len(state.messages)})"
        )
        self._tagged(tag, "OK", "STATUS completed")

    def _uid(self, tag: str, rest: str, server: "MockIMAPServer") -> None:
        sub_parts = rest.split(" ", 1)
        sub = sub_parts[0].upper()
        sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""
        if sub == "SEARCH":
            self._uid_search(tag, server)
        elif sub == "FETCH":
            self._uid_fetch(tag, sub_rest, server)
        elif sub in ("STORE", "COPY", "MOVE", "EXPUNGE"):
            self._tagged(tag, "NO", f"只读邮箱拒绝 UID {sub}")
        else:
            self._tagged(tag, "BAD", f"协议子集不支持 UID {sub}")

    def _uid_search(self, tag: str, server: "MockIMAPServer") -> None:
        uids = " ".join(str(uid) for uid, _ in server.state.messages)
        self._send_line(f"* SEARCH {uids}".rstrip())
        self._tagged(tag, "OK", "UID SEARCH completed")

    def _uid_fetch(self, tag: str, rest: str, server: "MockIMAPServer") -> None:
        # rest 形如: 1:2 (UID FLAGS RFC822) 或 1,3 (UID RFC822)
        spec = rest.split("(", 1)
        if not spec or not spec[0].strip():
            self._tagged(tag, "BAD", "UID FETCH 缺少 UID 集合")
            return
        set_spec = spec[0].strip()
        want_rfc822 = "RFC822" in rest.upper()
        want_flags = "FLAGS" in rest.upper()
        want_uid = "UID" in rest.upper()
        selected = self._resolve_uid_set(set_spec, server.state)
        if selected is None:
            self._tagged(tag, "BAD", f"无法解析 UID 集合: {set_spec}")
            return
        for seq, (uid, data) in enumerate(selected, start=1):
            # 构造 imaplib 可解析的 FETCH 响应：
            # * <seq> FETCH (FLAGS (...) UID <uid> RFC822 {<n>} CRLF <n 字节> CRLF )
            head = f"* {seq} FETCH ("
            params = []
            if want_flags:
                params.append("FLAGS (\\Seen)")
            if want_uid:
                params.append(f"UID {uid}")
            self.wfile.write(head.encode("utf-8"))
            self.wfile.write(" ".join(params).encode("utf-8"))
            if params:
                self.wfile.write(b" ")
            if want_rfc822:
                self.wfile.write(b"RFC822 ")
                self._send_literal(data)  # {n}\r\n，imaplib 随即精确读 n 字节
                self.wfile.write(data)
                # literal 数据后紧跟结束参数与 CRLF，不能单独成行
            self.wfile.write(b")" + CRLF)
            self.wfile.flush()
        self._tagged(tag, "OK", "UID FETCH completed")

    @staticmethod
    def _resolve_uid_set(set_spec: str, state: MailboxState):
        by_uid = {uid: data for uid, data in state.messages}
        result: list[tuple[int, bytes]] = []
        for token in set_spec.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                lo_s, hi_s = token.split(":", 1)
                try:
                    lo = 1 if lo_s == "*" else int(lo_s)
                    hi = max(by_uid) if hi_s == "*" else int(hi_s)
                except ValueError:
                    return None
                for uid in sorted(by_uid):
                    if lo <= uid <= hi:
                        result.append((uid, by_uid[uid]))
            elif token == "*":
                if by_uid:
                    uid = max(by_uid)
                    result.append((uid, by_uid[uid]))
            else:
                try:
                    uid = int(token)
                except ValueError:
                    return None
                if uid in by_uid:
                    result.append((uid, by_uid[uid]))
        # 去重并保持 UID 顺序
        dedup = {uid: data for uid, data in result}
        return sorted(dedup.items())


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MockIMAPServer(_ThreadingTCPServer):
    def __init__(self, state: MailboxState, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), _IMAPHandler)
        self.state = state
        self._log: list[str] = []
        self._log_lock = threading.Lock()
        self._clients: set[_IMAPHandler] = set()

    @property
    def address(self) -> tuple[str, int]:
        return self.server_address  # type: ignore[return-value]

    def log(self, line: str) -> None:
        with self._log_lock:
            self._log.append(line)

    def command_log(self) -> list[str]:
        with self._log_lock:
            return list(self._log)

    def register_client(self, client: _IMAPHandler) -> None:
        self._clients.add(client)

    def unregister_client(self, client: _IMAPHandler) -> None:
        self._clients.discard(client)


def start_server(state: MailboxState, host: str = "127.0.0.1",
                 port: int = 0) -> MockIMAPServer:
    server = MockIMAPServer(state, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
