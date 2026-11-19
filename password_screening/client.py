"""Screening client.

The client derives the digest locally, sends only the prefix, and compares the
suffix against the returned page entries.  Padding entries are ignored no
matter what suffix they carry.  A traversal always reads exactly one version;
if the server reports that the pinned version was retired, the client throws
away everything it has read and starts over.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .digest import (
    DIGEST_LENGTH,
    normalize_digest,
    password_digest,
    validate_prefix_length,
    validate_page_size,
)
from .errors import (
    DigestError,
    InvalidPrefix,
    ProtocolError,
    RestartRequired,
    ScreeningError,
    VersionUnavailable,
)
from .version import PADDING, RECORD

MAX_RESTARTS = 3
MAX_PAGES = 10_000


class Transport(Protocol):
    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


class LocalTransport:
    """In-process transport backed by a :class:`ScreeningService`."""

    def __init__(self, service: Any) -> None:
        handle = getattr(service, "handle", None)
        if not callable(handle):
            raise DigestError("LocalTransport requires a ScreeningService-like object")
        self._handle = handle
        self.calls: list[dict[str, Any]] = []

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = dict(payload)
        self.calls.append(snapshot)
        return self._handle(snapshot)


class HTTPTransport:
    """Minimal JSON-over-HTTP transport (``POST /lookup``)."""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(dict(payload)).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/lookup",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:  # error payloads still carry JSON
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise ScreeningError(f"screening service unreachable: {exc.reason}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("screening service returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProtocolError("screening service returned a non-object response")
        return decoded


@dataclass(frozen=True)
class ScreenResult:
    """Outcome of one password screening."""

    matched: bool
    version_id: str
    pages_read: int
    restarts: int
    entries_inspected: int

    def __bool__(self) -> bool:
        return self.matched


PageCallback = Callable[[int, Mapping[str, Any]], None]


class ScreeningClient:
    """Client side of the prefix/suffix screening protocol."""

    def __init__(
        self,
        transport: Transport,
        *,
        prefix_length: int = 5,
        expected_page_size: int | None = None,
        max_restarts: int = MAX_RESTARTS,
    ) -> None:
        if not callable(getattr(transport, "request", None)):
            raise DigestError("transport must provide a request(payload) method")
        self.transport = transport
        self.prefix_length = validate_prefix_length(prefix_length)
        self.expected_page_size = (
            validate_page_size(expected_page_size) if expected_page_size is not None else None
        )
        if isinstance(max_restarts, bool) or not isinstance(max_restarts, int) or max_restarts < 0:
            raise DigestError("max_restarts must be a non-negative integer")
        self.max_restarts = max_restarts

    # -- public API ---------------------------------------------------------
    def screen(self, password: str, *, on_page: PageCallback | None = None) -> ScreenResult:
        """Screen ``password``; never leaves this method on a transient restart."""

        return self.screen_digest(password_digest(password), on_page=on_page)

    def screen_digest(
        self, digest: str, *, on_page: PageCallback | None = None
    ) -> ScreenResult:
        """Screen a precomputed 64-character digest."""

        normalized = normalize_digest(digest)
        prefix = normalized[: self.prefix_length]
        secret_suffix = normalized[self.prefix_length :]

        restarts = 0
        pinned: str | None = None
        while True:
            try:
                return self._traverse(prefix, secret_suffix, restarts, pinned, on_page)
            except _Restart as signal:
                restarts += 1
                if restarts > self.max_restarts:
                    raise RestartRequired(
                        f"service kept restarting the traversal: {signal.reason}",
                        signal.current_version,
                    ) from None
                # Restart from page 0 against the version the service named.
                # Nothing read from the discarded traversal is reused.
                pinned = signal.current_version

    # -- traversal ----------------------------------------------------------
    def _traverse(
        self,
        prefix: str,
        secret_suffix: str,
        restarts: int,
        pinned: str | None,
        on_page: PageCallback | None,
    ) -> ScreenResult:
        total_pages: int | None = None
        page_size: int | None = None
        matched = False
        entries_inspected = 0
        page_index = 0

        while True:
            if page_index >= MAX_PAGES:  # pragma: no cover - defensive
                raise ProtocolError("page count exceeded the safety limit")
            payload: dict[str, Any] = {"op": "page", "prefix": prefix, "page": page_index}
            if pinned is not None:
                payload["version"] = pinned

            response = self._request(payload)
            if response.get("restart_required"):
                raise _Restart(
                    str(response.get("message", "service requested a restart")),
                    _optional_str(response.get("current_version")),
                )
            if not response.get("ok", False):
                if response.get("error") == "version_unavailable":
                    raise _Restart(
                        str(response.get("message", "pinned version unavailable")),
                        _optional_str(response.get("current_version")),
                    )
                self._raise_error(response)

            if on_page is not None:
                on_page(page_index, response)

            served = response.get("served_version")
            if not isinstance(served, str) or not served:
                raise ProtocolError("response is missing served_version")
            if pinned is None:
                pinned = served
            elif served != pinned:
                raise ProtocolError(
                    f"response served version {served!r} but version {pinned!r} was pinned"
                )

            if response.get("prefix") != prefix:
                raise ProtocolError("response prefix does not match the requested prefix")
            if response.get("page") != page_index:
                raise ProtocolError("response page number does not match the requested page")

            response_page_size = response.get("page_size")
            response_total_pages = response.get("total_pages")
            if not isinstance(response_page_size, int) or response_page_size <= 0:
                raise ProtocolError("response page_size must be a positive integer")
            if not isinstance(response_total_pages, int) or response_total_pages <= 0:
                raise ProtocolError("response total_pages must be a positive integer")
            if self.expected_page_size is not None and response_page_size != self.expected_page_size:
                raise ProtocolError(
                    f"response page_size {response_page_size} does not match the "
                    f"configured {self.expected_page_size}"
                )
            if page_size is None:
                page_size = response_page_size
                total_pages = response_total_pages
            elif response_page_size != page_size:
                raise ProtocolError("page_size changed within one version")
            elif response_total_pages != total_pages:
                raise ProtocolError("total_pages changed within one version")
            if page_index >= total_pages:
                raise ProtocolError(
                    f"response announced page {page_index} outside a {total_pages}-page version"
                )

            entries = response.get("entries")
            if not isinstance(entries, list):
                raise ProtocolError("response entries must be a list")
            if len(entries) != page_size:
                raise ProtocolError(
                    f"response carried {len(entries)} entries, expected {page_size}"
                )

            for entry in entries:
                kind, suffix = self._parse_entry(entry, prefix)
                entries_inspected += 1
                # Padding is a decoy: it must never turn into a hit even if a
                # misbehaving service fills it with the queried suffix.
                if kind == RECORD and suffix == secret_suffix:
                    matched = True

            page_index += 1
            if total_pages is not None and page_index >= total_pages:
                break

        return ScreenResult(
            matched=matched,
            version_id=pinned or "",
            pages_read=page_index,
            restarts=restarts,
            entries_inspected=entries_inspected,
        )

    # -- helpers ------------------------------------------------------------
    def _request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self.transport.request(payload)
        except ScreeningError:
            raise
        except Exception as exc:  # transport failures become protocol errors
            raise ProtocolError(f"transport failed: {exc}") from exc
        if not isinstance(response, Mapping):
            raise ProtocolError("transport returned a non-object response")
        return dict(response)

    @staticmethod
    def _parse_entry(entry: Any, prefix: str) -> tuple[str, str]:
        if not isinstance(entry, Mapping):
            raise ProtocolError("page entry must be an object")
        kind = entry.get("kind")
        suffix = entry.get("suffix")
        if kind not in {RECORD, PADDING}:
            raise ProtocolError(f"unknown page entry kind {kind!r}")
        if not isinstance(suffix, str) or len(suffix) != DIGEST_LENGTH - len(prefix):
            raise ProtocolError("page entry suffix has the wrong length")
        if suffix != suffix.upper():
            raise ProtocolError("page entry suffix must be uppercase hexadecimal")
        try:
            int(suffix, 16)
        except ValueError as exc:
            raise ProtocolError("page entry suffix must be hexadecimal") from exc
        return kind, suffix

    @staticmethod
    def _raise_error(response: Mapping[str, Any]) -> None:
        code = response.get("error", "unknown")
        message = response.get("message", "screening service rejected the request")
        if code == "invalid_prefix":
            raise InvalidPrefix(f"service rejected the prefix: {message}")
        if code == "page_out_of_range":
            raise ProtocolError(f"service reported a page out of range: {message}")
        if code == "no_current_version":
            raise ScreeningError(f"service has no digest version published: {message}")
        if code == "version_unavailable":
            raise VersionUnavailable(str(response.get("requested_version", "")), None)
        raise ProtocolError(f"service error {code!r}: {message}")


class _Restart(Exception):
    def __init__(self, reason: str, current_version: str | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current_version = current_version


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "HTTPTransport",
    "LocalTransport",
    "ScreenResult",
    "ScreeningClient",
    "Transport",
]
