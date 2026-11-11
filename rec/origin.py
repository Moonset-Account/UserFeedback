"""本地 HTTP 源站（公开接口，采集器唯一的数据来源）。

源站挂载在应用端口的 ``/origin/`` 路径下，对外只暴露标准 HTTP 语义：
``HEAD /origin/objects/<对象名>`` 与带 ``Range``/``If-Range`` 的 GET。

可模拟的行为（每个场景有固定的故障预算，演示结束后自动恢复正常）：
- 正常 206 分段响应与强 ETag；
- 首次分段传输中途断开（连接重置，只送出部分字节）；
- 续取时对象被替换（ETag 变化，旧 If-Range 失效 → 200 回退）；
- 忽略 Range 直接返回 200 全量；
- 416（区间不可满足，带 ``Content-Range: bytes */<size>``）；
- 不一致的 Content-Range（声明区间与实际字节不符，或总长度字段错误）；
- 错误区间返回 500；
- Content-Length 与实际字节数不符。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

OBJECT_PATH_RE = re.compile(r"^/origin/objects/([A-Za-z0-9._-]+)$")
TOKEN_PATH_RE = re.compile(
    r"^/origin/s/([A-Za-z0-9_-]{6,})/([A-Za-z0-9._-]+)$"
)


def _etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest() + '"'


def parse_range_header(value: Optional[str], length: int) -> Tuple[int, int]:
    """解析单个字节区间为闭区间 [start, end]；非法时抛出 ValueError。"""
    if not value:
        raise ValueError("缺少 Range 头")
    match = RANGE_RE.match(value.strip())
    if not match:
        raise ValueError(f"仅支持单区间 bytes=start-end，收到 {value!r}")
    left, right = match.groups()
    if left == "" and right == "":
        raise ValueError("Range 区间两端均为空")
    if left == "":
        # 后缀形式 bytes=-N：最后 N 字节
        suffix = int(right)
        if suffix <= 0:
            raise ValueError("后缀区间长度必须为正")
        return max(0, length - suffix), length - 1
    start = int(left)
    end = int(right) if right else length - 1
    if start >= length:
        raise ValueError("起点超出对象长度")
    if end >= length:
        end = length - 1
    if end < start:
        raise ValueError("区间终点小于起点")
    return start, end


@dataclass
class ScenarioResult:
    kind: str
    data: Optional[bytes] = None
    start: int = -1
    end: int = -1
    total: int = -1
    note: str = ""
    status: int = -1


class Scenario:
    """场景状态机：依据自身计数决定下一次 GET 的行为。"""

    name = "base"

    def __init__(self, objects: Dict[str, bytes], initial: str,
                 replacement: Optional[str] = None):
        self._objects = objects
        self.current = initial
        self.replacement = replacement
        self.calls = 0
        self.lock = threading.Lock()

    def object_bytes(self) -> bytes:
        return self._objects[self.current]

    def head(self) -> ScenarioResult:
        data = self.object_bytes()
        return ScenarioResult("head", data=data, total=len(data))

    # 子类覆盖下面的方法；kind 取值见各子类
    def handle(self, range_header: Optional[str],
               if_range: Optional[str]) -> ScenarioResult:
        data = self.object_bytes()
        length = len(data)
        start, end = parse_range_header(range_header, length)
        if if_range and if_range != _etag(data):
            # RFC 9110：If-Range 校验失败 → 回退 200 全量
            return ScenarioResult(
                "full", data=data, total=length,
                note=f"If-Range={if_range} 与当前 ETag 不一致，回退 200 全量响应",
            )
        return ScenarioResult(
            "partial", data=data[start : end + 1], start=start, end=end, total=length,
            note="正常 206 分段响应",
        )

    def tick(self) -> int:
        with self.lock:
            self.calls += 1
            return self.calls


class BenignScenario(Scenario):
    name = "benign"


class DropFirstScenario(Scenario):
    """首个 GET 在送出一半字节后断开连接，之后全部正常。"""

    name = "drop-first"

    def handle(self, range_header, if_range):
        call = self.tick()
        base = super().handle(range_header, if_range)
        if call != 1 or base.kind != "partial":
            return base
        cut = max(1, len(base.data) // 2)
        return ScenarioResult(
            "truncate", data=base.data[:cut], start=base.start,
            # 保留原始终点：源站声明完整区间长度，却只送出一半即断连
            end=base.end, total=base.total,
            note="模拟传输中断：仅送出声明区间前一半字节后关闭连接",
        )


class SwapScenario(Scenario):
    """第 2 次 GET 时对象被替换为新版本，之后保持新版本。"""

    name = "swap"

    def handle(self, range_header, if_range):
        call = self.tick()
        if call == 2 and self.replacement and self.current != self.replacement:
            self.current = self.replacement
            data = self.object_bytes()
            start, end = parse_range_header(range_header, len(data))
            # 替换发生在 If-Range 校验之前的源站行为：仍按新区间返回 206，
            # 但 ETag 与请求者的 If-Range 不一致，采集器必须识别版本混杂。
            return ScenarioResult(
                "partial", data=data[start : end + 1], start=start, end=end,
                total=len(data),
                note="源站已用新版本替换对象，却仍对旧 If-Range 返回 206",
            )
        return super().handle(range_header, if_range)


class ErrorRegionScenario(Scenario):
    """前两次 GET 返回 500，第三次对越界 Range 返回 416，之后恢复正常。"""

    name = "error-region"

    def handle(self, range_header, if_range):
        call = self.tick()
        data = self.object_bytes()
        if call <= 2:
            return ScenarioResult(
                "error", status=500, total=len(data),
                note="模拟服务器错误区间：HTTP 500",
            )
        if call == 3:
            length = len(data)
            return ScenarioResult(
                "range-unsatisfiable", status=416, total=length,
                note="模拟不可满足区间：HTTP 416",
            )
        return super().handle(range_header, if_range)


class IgnoreRangeScenario(Scenario):
    """前两次 GET 忽略 Range 返回 200，之后恢复 206。"""

    name = "ignore-range"

    def handle(self, range_header, if_range):
        call = self.tick()
        data = self.object_bytes()
        if call <= 2:
            return ScenarioResult(
                "full", data=data, total=len(data),
                note="源站忽略 Range 请求头，返回 200 全量对象",
            )
        return super().handle(range_header, if_range)


class BadContentRangeScenario(Scenario):
    """前两次 GET 返回字节正确但 Content-Range 声明错误的 206。"""

    name = "bad-content-range"

    def handle(self, range_header, if_range):
        call = self.tick()
        base = super().handle(range_header, if_range)
        if call <= 2 and base.kind == "partial":
            # 字节本身属于 [start, end]，但声明的总长度错误 → 采集器必须拒绝
            return ScenarioResult(
                "inconsistent-range", data=base.data, start=base.start,
                end=base.end, total=base.total + 999,
                note="模拟不一致 Content-Range：总长度字段与对象真实长度不符",
            )
        return base


class BadLengthScenario(Scenario):
    """前两次 GET 声明的 Content-Length 多于实际送出字节后断连。"""

    name = "bad-length"

    def handle(self, range_header, if_range):
        call = self.tick()
        base = super().handle(range_header, if_range)
        if call <= 2 and base.kind == "partial":
            cut = max(1, len(base.data) // 2)
            return ScenarioResult(
                "truncate", data=base.data[:cut], start=base.start,
                end=base.end, total=base.total,
                note="声明 Content-Length 为完整区间长度，实际只送出一半即断连",
            )
        return base


SCENARIO_CLASSES: Dict[str, type] = {
    cls.name: cls
    for cls in (
        BenignScenario,
        DropFirstScenario,
        SwapScenario,
        ErrorRegionScenario,
        IgnoreRangeScenario,
        BadContentRangeScenario,
        BadLengthScenario,
    )
}


class OriginServer:
    """持有对象字节与场景状态，供 HTTP handler 调用。"""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.scenarios: Dict[str, Scenario] = {}
        self.lock = threading.Lock()

    def load_objects(self, objects_dir: str) -> None:
        with self.lock:
            self.objects.clear()
            for name in sorted(os.listdir(objects_dir)):
                if name.endswith(".expb"):
                    with open(os.path.join(objects_dir, name), "rb") as fh:
                        self.objects[name] = fh.read()

    def configure(self, token: str, object_name: str, scenario_name: str,
                  replacement: Optional[str] = None) -> str:
        with self.lock:
            if object_name not in self.objects:
                raise KeyError(object_name)
            if replacement and replacement not in self.objects:
                raise KeyError(replacement)
            cls = SCENARIO_CLASSES[scenario_name]
            scenario = cls(self.objects, object_name, replacement)
            self.scenarios[token] = scenario
        return token

    def reset(self, token: Optional[str] = None) -> None:
        with self.lock:
            if token:
                self.scenarios.pop(token, None)
            else:
                self.scenarios.clear()



class OriginMethods:
    """可与任意 BaseHTTPRequestHandler 子类组合的源站处理方法。"""

    origin: "OriginServer"

    def origin_not_found(self) -> None:
        # HTTP 状态行只能使用 latin-1，错误说明保持 ASCII
        self.send_error(404, "Object Not Found")

    def origin_resolve(self) -> Optional[Tuple[str, Scenario]]:
        path = self.path.split("?", 1)[0]
        token_match = TOKEN_PATH_RE.match(path)
        if token_match:
            token, _name = token_match.groups()
            with self.origin.lock:
                scenario = self.origin.scenarios.get(token)
            if scenario is None:
                self.send_error(404, "Scenario Not Found Or Expired")
                return None
            return token, scenario
        match = OBJECT_PATH_RE.match(path)
        if not match:
            self.origin_not_found()
            return None
        name = match.group(1)
        with self.origin.lock:
            scenario = self.origin.scenarios.get(name)
            if scenario is None:
                if name not in self.origin.objects:
                    self.origin_not_found()
                    return None
                scenario = BenignScenario(self.origin.objects, name)
                self.origin.scenarios[name] = scenario
        return name, scenario

    def origin_note_header(self, note: str) -> None:
        # 非标准调试头，仅用于界面展示“来源证据”，采集器不依赖它做判定
        safe = note.encode("unicode_escape")[:200].decode("ascii", "replace")
        self.send_header("X-Scenario-Note", safe)

    def origin_write_all(self, payload: bytes) -> None:
        self.wfile.write(payload)
        self.wfile.flush()

    def origin_emit(self, result: ScenarioResult, scenario: Scenario) -> None:
        data = scenario.object_bytes()
        etag = _etag(data)

        if result.kind in ("error", "range-unsatisfiable"):
            self.send_response(result.status)
            if result.status == 416:
                self.send_header("Content-Range", f"bytes */{result.total}")
            self.send_header("Content-Length", "0")
            self.send_header("ETag", etag)
            self.origin_note_header(result.note)
            self.end_headers()
            return

        assert result.data is not None
        payload = result.data
        if result.kind == "full":
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.origin_note_header(result.note)
            self.end_headers()
            self.origin_write_all(payload)
            return

        if result.kind in ("partial", "inconsistent-range"):
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {result.start}-{result.end}/{result.total}",
            )
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.origin_note_header(result.note)
            self.end_headers()
            self.origin_write_all(payload)
            return

        if result.kind == "truncate":
            self.send_response(206)
            declared_length = result.end - result.start + 1
            self.send_header(
                "Content-Range",
                f"bytes {result.start}-{result.end}/{result.total}",
            )
            # 故意声明完整区间长度，只发送部分字节后关闭
            self.send_header("Content-Length", str(declared_length))
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.origin_note_header(result.note)
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            # 直接关闭底层连接，制造 IncompleteRead
            self.close_connection = True
            return

    def handle_origin_head(self) -> None:
        resolved = self.origin_resolve()
        if resolved is None:
            return
        _, scenario = resolved
        result = scenario.head()
        data = result.data
        assert data is not None
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", _etag(data))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", "Wed, 01 Jan 2026 00:00:00 GMT")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def handle_origin_get(self) -> None:
        resolved = self.origin_resolve()
        if resolved is None:
            return
        _, scenario = resolved
        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        try:
            result = scenario.handle(range_header, if_range)
        except ValueError as exc:
            data = scenario.object_bytes()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(data)}")
            self.send_header("Content-Length", "0")
            self.send_header("ETag", _etag(data))
            self.origin_note_header(f"416: {exc}")
            self.end_headers()
            return
        self.origin_emit(result, scenario)


def make_handler(origin: OriginServer) -> type:
    class OriginHandler(OriginMethods, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            return

        def do_HEAD(self) -> None:  # noqa: N802
            self.handle_origin_head()

        def do_GET(self) -> None:  # noqa: N802
            self.handle_origin_get()

    OriginHandler.origin = origin
    return OriginHandler


def build_server(origin: OriginServer, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(origin))
    return server
