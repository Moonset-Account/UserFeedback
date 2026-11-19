"""Version retention and atomic publication.

A ``VersionStore`` keeps a bounded history of immutable versions.  Publishing a
new version swaps the current pointer in one step, so a lookup either reads the
whole old version or the whole new one - never a mixture.
"""

from __future__ import annotations

import threading

from .digest import validate_page_size, validate_prefix_length
from .errors import DigestError, NoCurrentVersion, RestartRequired, VersionUnavailable
from .version import DigestVersion, PageEntry, build_version


class VersionStore:
    """Thread-safe store of digest versions with atomic publication."""

    def __init__(
        self,
        *,
        prefix_length: int = 5,
        page_size: int = 10,
        retain: int = 4,
    ) -> None:
        self.prefix_length = validate_prefix_length(prefix_length)
        self.page_size = validate_page_size(page_size)
        if isinstance(retain, bool) or not isinstance(retain, int) or retain < 2:
            raise DigestError("retain must be an integer >= 2")
        self.retain = retain
        self._versions: dict[str, DigestVersion] = {}
        self._order: list[str] = []
        self._current: DigestVersion | None = None
        self._lock = threading.RLock()

    # -- publication --------------------------------------------------------
    def publish(self, digests, *, version_id: str | None = None) -> DigestVersion:
        """Build and atomically publish a version; returns the published version."""

        version = build_version(
            digests,
            prefix_length=self.prefix_length,
            page_size=self.page_size,
            version_id=version_id,
        )
        self.publish_version(version)
        return version

    def publish_version(self, version: DigestVersion) -> None:
        if not isinstance(version, DigestVersion):
            raise DigestError("publish_version expects a DigestVersion")
        if version.prefix_length != self.prefix_length:
            raise DigestError(
                f"version prefix_length {version.prefix_length} does not match "
                f"store prefix_length {self.prefix_length}"
            )
        if version.page_size != self.page_size:
            raise DigestError(
                f"version page_size {version.page_size} does not match "
                f"store page_size {self.page_size}"
            )
        with self._lock:
            if version.version_id in self._versions:
                raise DigestError(f"version {version.version_id!r} was already published")
            self._versions[version.version_id] = version
            self._order.append(version.version_id)
            self._current = version
            while len(self._order) > self.retain:
                expired = self._order.pop(0)
                self._versions.pop(expired, None)

    # -- reads --------------------------------------------------------------
    @property
    def current(self) -> DigestVersion:
        with self._lock:
            if self._current is None:
                raise NoCurrentVersion("no version has been published yet")
            return self._current

    @property
    def current_version_id(self) -> str | None:
        with self._lock:
            return self._current.version_id if self._current is not None else None

    def retained_versions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._order)

    def get(self, version_id: str | None) -> DigestVersion:
        """Resolve a pinned version id (``None`` means ``current``)."""

        with self._lock:
            if version_id is None:
                return self.current
            version = self._versions.get(version_id)
            if version is None:
                raise VersionUnavailable(version_id, self._current)
            return version

    def lookup_page(
        self,
        prefix: str,
        page: int,
        *,
        version_id: str | None = None,
    ) -> tuple[DigestVersion, tuple[PageEntry, ...]]:
        """Read one page from exactly one version.

        A retained version - even one that is no longer current - is served in
        full, so an in-progress traversal can finish the version it started
        on.  A pinned version that has been evicted is never replaced by the
        current one: the store answers with ``RestartRequired`` so the client
        discards every page it already read.
        """

        with self._lock:
            current = self._current
            if current is None:
                raise NoCurrentVersion("no version has been published yet")
            if version_id is None:
                version = current
            else:
                version = self._versions.get(version_id)
                if version is None:
                    raise RestartRequired(
                        f"pinned version {version_id!r} was retired before the "
                        "traversal completed",
                        current.version_id,
                    )
            return version, version.page(prefix, page)


__all__ = ["VersionStore"]
