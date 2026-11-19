"""Request/response screening service.

The service speaks plain JSON dictionaries so it can be driven by an in-process
transport in tests or by a small HTTP server.  It never sees the digest suffix:
requests carry only a prefix, a page number and a pinned version id.
"""

from __future__ import annotations

import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from .errors import (
    DigestError,
    InvalidPrefix,
    NoCurrentVersion,
    PageOutOfRange,
    ProtocolError,
    RestartRequired,
    ScreeningError,
    VersionUnavailable,
)
from .store import VersionStore
from .version import ENTRY_KINDS

MAX_REQUEST_BYTES = 4096


def _error_code(exc: ScreeningError) -> str:
    mapping = {
        InvalidPrefix: "invalid_prefix",
        DigestError: "invalid_request",
        PageOutOfRange: "page_out_of_range",
        VersionUnavailable: "version_unavailable",
        NoCurrentVersion: "no_current_version",
        RestartRequired: "restart_required",
        ProtocolError: "protocol_error",
    }
    for kind, code in mapping.items():
        if isinstance(exc, kind):
            return code
    return "internal_error"


class ScreeningService:
    """Stateless front door over a :class:`VersionStore`."""

    def __init__(self, store: VersionStore) -> None:
        if not isinstance(store, VersionStore):
            raise DigestError("ScreeningService requires a VersionStore")
        self.store = store

    @property
    def prefix_length(self) -> int:
        return self.store.prefix_length

    @property
    def page_size(self) -> int:
        return self.store.page_size

    # -- wire contract ------------------------------------------------------
    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one JSON request dictionary and return a JSON response."""

        try:
            return self._handle(request)
        except ScreeningError as exc:
            response: dict[str, Any] = {
                "ok": False,
                "error": _error_code(exc),
                "message": str(exc),
            }
            if isinstance(exc, RestartRequired):
                response["restart_required"] = True
                response["current_version"] = exc.current_version
            elif isinstance(exc, VersionUnavailable):
                response["requested_version"] = exc.requested
                if exc.current is not None:
                    response["current_version"] = exc.current.version_id
            return response

    def _handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise ProtocolError("request must be a JSON object")
        op = request.get("op", "page")
        if op == "info":
            return self._info(request)
        if op != "page":
            raise ProtocolError(f"unknown operation {op!r}")
        return self._page(request)

    def _info(self, request: Mapping[str, Any]) -> dict[str, Any]:
        version = self.store.get(_optional_version(request))
        payload = {
            "ok": True,
            "op": "info",
            "current_version": self.store.current_version_id,
            "page_size": version.page_size,
            "total_pages": version.total_pages,
            "prefix_length": version.prefix_length,
            "digest_count": version.digest_count,
        }
        payload.update({"served_version": version.version_id})
        return payload

    def _page(self, request: Mapping[str, Any]) -> dict[str, Any]:
        prefix = request.get("prefix")
        if not isinstance(prefix, str):
            raise InvalidPrefix("prefix is required and must be a string")
        page = request.get("page", 0)
        if isinstance(page, bool) or not isinstance(page, int):
            raise ProtocolError("page must be an integer")

        prefix_length = request.get("prefix_length", self.prefix_length)
        if prefix_length != self.prefix_length:
            raise InvalidPrefix(
                f"prefix_length must be {self.prefix_length}, got {prefix_length!r}"
            )

        version, entries = self.store.lookup_page(
            prefix, page, version_id=_optional_version(request)
        )
        response = {
            "ok": True,
            "op": "page",
            "served_version": version.version_id,
            "prefix": prefix.upper(),
            "page": page,
            "page_size": version.page_size,
            "total_pages": version.total_pages,
            "entries": [entry.to_dict() for entry in entries],
        }
        for entry in response["entries"]:
            if entry["kind"] not in ENTRY_KINDS:  # pragma: no cover - defensive
                raise ProtocolError(f"service produced an unknown entry kind {entry['kind']!r}")
        if len(response["entries"]) != version.page_size:  # pragma: no cover
            raise ProtocolError("service produced a page with the wrong number of entries")
        return response


def _optional_version(request: Mapping[str, Any]) -> str | None:
    version = request.get("version")
    if version is None:
        return None
    if not isinstance(version, str) or not version:
        raise ProtocolError("version must be a non-empty string when provided")
    return version


# -- HTTP front end ---------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    service: ScreeningService
    server_version = "ScreeningService/1.0"

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/lookup":
            self._send({"ok": False, "error": "not_found", "message": "unknown path"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send({"ok": False, "error": "invalid_request"}, 400)
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send({"ok": False, "error": "invalid_request"}, 400)
            return
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send({"ok": False, "error": "invalid_request"}, 400)
            return
        if not isinstance(request, dict):
            self._send({"ok": False, "error": "invalid_request"}, 400)
            return
        response = self.service.handle(request)
        status = 200 if response.get("ok") else 409 if response.get("restart_required") else 400
        self._send(response, status)

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None


def serve(store: VersionStore, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create (but do not start) an HTTP server exposing ``store`` on /lookup."""

    service = ScreeningService(store)
    handler = type("BoundScreeningHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


__all__ = ["ScreeningService", "serve"]
