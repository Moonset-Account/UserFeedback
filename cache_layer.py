"""Revocation-safe graded response cache.

Two views of the same resource are supported:

* ``summary`` -- a public digest.  It may enter the *tenant-level* cache only
  when the loader explicitly marks the load result as ``shareable``.
* ``detail`` -- a restricted payload.  It is cached *per principal* and every
  cache hit re-validates the caller's *current* permission version against
  the version stamped on the entry.

Safety rules implemented here:

1. The caller supplies an already-verified identity (tenant + principal),
   the resource version and the requested view; the cache never invents
   identity or authorization data of its own.
2. A detail entry is served from cache only when its stamped permission
   version still equals the current one reported by the authorization
   service.
3. When a load that started *before* a permission revocation finishes
   *after* it (a late old request), the stale detail is never written back
   into the usable cache: the permission version observed at load start is
   re-checked before the write, and the request is denied.
4. When a refresh fails, a still-valid (unexpired) summary may be reused;
   a restricted detail is reused only when the authorization check still
   passes, otherwise the request is denied.
5. Every request appends a :class:`ViewRecord` describing which source
   served (or denied) the view.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


class RefreshError(Exception):
    """The backing loader failed to refresh a resource."""


class AuthorizationError(Exception):
    """The caller's current permission version does not authorize the view."""


VIEW_SUMMARY = "summary"
VIEW_DETAIL = "detail"

SOURCE_LOADER = "loader"
SOURCE_SUMMARY_CACHE = "summary_cache"
SOURCE_DETAIL_CACHE = "detail_cache"
SOURCE_SUMMARY_FALLBACK = "summary_fallback_refresh_failed"
SOURCE_DETAIL_FALLBACK = "detail_fallback_refresh_failed"
SOURCE_DENIED_PERM_CHANGED = "denied_perm_version_changed"
SOURCE_DENIED_REFRESH = "denied_refresh_failed"


@dataclass(frozen=True)
class Identity:
    """An already-verified caller identity."""

    tenant: str
    principal: str


@dataclass(frozen=True)
class LoadResult:
    """What a loader returns for one resource load.

    ``perm_version`` is the permission version of the requesting principal
    observed when the load *started*; it is re-validated before any detail
    is written to or served from the cache.
    """

    resource_id: str
    resource_version: int
    perm_version: int
    shareable: bool
    summary: dict
    detail: dict


@dataclass(frozen=True)
class ViewRecord:
    """Audit record: which source served (or denied) one request."""

    seq: int
    tenant: str
    principal: str
    resource_id: str
    view: str
    source: str
    ok: bool
    detail: str


@dataclass
class _SummaryEntry:
    resource_version: int
    payload: dict
    expires_at: float


@dataclass
class _DetailEntry:
    resource_version: int
    perm_version: int
    payload: dict
    expires_at: float


class AuthorizationService:
    """Tracks the current permission version per (tenant, principal).

    A revocation simply bumps the version; any cache entry or in-flight
    load stamped with an older version immediately becomes unusable for
    details.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: Dict[Tuple[str, str], int] = {}

    def set_version(self, tenant: str, principal: str, version: int) -> None:
        with self._lock:
            self._versions[(tenant, principal)] = version

    def current_version(self, tenant: str, principal: str) -> int:
        with self._lock:
            return self._versions.get((tenant, principal), 0)

    def revoke(self, tenant: str, principal: str) -> int:
        """Bump the permission version; returns the new version."""
        with self._lock:
            key = (tenant, principal)
            self._versions[key] = self._versions.get(key, 0) + 1
            return self._versions[key]


# A loader is called outside the cache lock and must return a LoadResult
# (or raise, which is treated as a refresh failure).
LoaderFn = Callable[[Identity, str, str], LoadResult]


class ResponseCache:
    """Graded cache: tenant-level summaries, per-principal details."""

    def __init__(
        self,
        auth: AuthorizationService,
        summary_ttl: float = 30.0,
        detail_ttl: float = 30.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._auth = auth
        self._summary_ttl = summary_ttl
        self._detail_ttl = detail_ttl
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        # (tenant, resource_id) -> summary entry
        self._summaries: Dict[Tuple[str, str], _SummaryEntry] = {}
        # (tenant, principal, resource_id) -> detail entry
        self._details: Dict[Tuple[str, str, str], _DetailEntry] = {}
        self._seq = itertools.count(1)
        self.records: List[ViewRecord] = []

    # ------------------------------------------------------------------ API

    def request(
        self,
        identity: Identity,
        resource_id: str,
        view: str,
        loader: LoaderFn,
        resource_version: Optional[int] = None,
    ) -> Tuple[dict, ViewRecord]:
        """Serve ``view`` of ``resource_id`` for ``identity``.

        Returns ``(payload, record)``.  Raises :class:`AuthorizationError`
        when the current permission version does not authorize a detail
        view, and :class:`RefreshError` when the loader fails and no valid
        cached fallback exists.
        """
        if view == VIEW_SUMMARY:
            return self._request_summary(identity, resource_id, resource_version, loader)
        if view == VIEW_DETAIL:
            return self._request_detail(identity, resource_id, resource_version, loader)
        raise ValueError(f"unknown view: {view!r}")

    # -------------------------------------------------------------- summary

    def _request_summary(self, identity, resource_id, resource_version, loader):
        key = (identity.tenant, resource_id)
        with self._lock:
            entry = self._summaries.get(key)
            if (
                entry is not None
                and entry.expires_at > self._clock()
                and (resource_version is None or entry.resource_version == resource_version)
            ):
                rec = self._record(
                    identity, resource_id, VIEW_SUMMARY, SOURCE_SUMMARY_CACHE, True,
                    f"tenant-level summary hit (resource_version={entry.resource_version})",
                )
                return dict(entry.payload), rec

        try:
            result = loader(identity, resource_id, VIEW_SUMMARY)
        except Exception as exc:
            # Refresh failed: reuse the summary only while it is still valid.
            with self._lock:
                entry = self._summaries.get(key)
                if entry is not None and entry.expires_at > self._clock():
                    rec = self._record(
                        identity, resource_id, VIEW_SUMMARY, SOURCE_SUMMARY_FALLBACK, True,
                        f"refresh failed ({exc!r}); reused still-valid summary "
                        f"(resource_version={entry.resource_version})",
                    )
                    return dict(entry.payload), rec
            rec = self._record(
                identity, resource_id, VIEW_SUMMARY, SOURCE_DENIED_REFRESH, False,
                f"refresh failed ({exc!r}) and no still-valid summary exists",
            )
            raise RefreshError(f"summary refresh failed for {resource_id!r}: {exc}") from exc

        with self._lock:
            note = self._store_summary_locked(identity, result)
        rec = self._record(identity, resource_id, VIEW_SUMMARY, SOURCE_LOADER, True, note)
        return dict(result.summary), rec

    def _store_summary_locked(self, identity, result: LoadResult) -> str:
        """Store the summary only when explicitly shareable.  Lock held."""
        if not result.shareable:
            return "loaded; summary NOT shareable -> bypassed tenant cache"
        key = (identity.tenant, result.resource_id)
        existing = self._summaries.get(key)
        if existing is not None and result.resource_version < existing.resource_version:
            return (
                f"loaded; stale resource_version {result.resource_version} < "
                f"cached {existing.resource_version} -> summary not overwritten"
            )
        self._summaries[key] = _SummaryEntry(
            resource_version=result.resource_version,
            payload=dict(result.summary),
            expires_at=self._clock() + self._summary_ttl,
        )
        return "loaded; shareable summary stored in tenant cache"

    # --------------------------------------------------------------- detail

    def _request_detail(self, identity, resource_id, resource_version, loader):
        key = (identity.tenant, identity.principal, resource_id)
        current_perm = self._auth.current_version(identity.tenant, identity.principal)
        with self._lock:
            entry = self._details.get(key)
            if (
                entry is not None
                and entry.expires_at > self._clock()
                and entry.perm_version == current_perm
                and (resource_version is None or entry.resource_version == resource_version)
            ):
                rec = self._record(
                    identity, resource_id, VIEW_DETAIL, SOURCE_DETAIL_CACHE, True,
                    f"per-principal detail hit; perm_version={entry.perm_version} "
                    f"re-validated against current",
                )
                return dict(entry.payload), rec

        try:
            result = loader(identity, resource_id, VIEW_DETAIL)
        except Exception as exc:
            # Refresh failed: a restricted detail is reused only when the
            # authorization check still passes.
            now_perm = self._auth.current_version(identity.tenant, identity.principal)
            with self._lock:
                entry = self._details.get(key)
                if (
                    entry is not None
                    and entry.expires_at > self._clock()
                    and entry.perm_version == now_perm
                ):
                    rec = self._record(
                        identity, resource_id, VIEW_DETAIL, SOURCE_DETAIL_FALLBACK, True,
                        f"refresh failed ({exc!r}); reused still-authorized detail "
                        f"(perm_version={entry.perm_version}, "
                        f"resource_version={entry.resource_version})",
                    )
                    return dict(entry.payload), rec
            rec = self._record(
                identity, resource_id, VIEW_DETAIL, SOURCE_DENIED_REFRESH, False,
                f"refresh failed ({exc!r}) and no still-authorized detail exists",
            )
            raise RefreshError(f"detail refresh failed for {resource_id!r}: {exc}") from exc

        # The load finished.  Re-validate the permission version observed at
        # load start against the *current* one: if a revocation raced with
        # this (late) request, the stale detail must never be written back
        # into the usable cache.
        now_perm = self._auth.current_version(identity.tenant, identity.principal)
        if result.perm_version != now_perm:
            with self._lock:
                # The public summary is not affected by the revocation; it
                # may still be stored when explicitly shareable.
                self._store_summary_locked(identity, result)
            rec = self._record(
                identity, resource_id, VIEW_DETAIL, SOURCE_DENIED_PERM_CHANGED, False,
                f"load stamped perm_version={result.perm_version} but current is "
                f"{now_perm}; stale detail discarded, cache not written",
            )
            raise AuthorizationError(
                f"permission version changed during load for "
                f"{identity.tenant}/{identity.principal}: "
                f"{result.perm_version} -> {now_perm}"
            )

        with self._lock:
            self._details[key] = _DetailEntry(
                resource_version=result.resource_version,
                perm_version=result.perm_version,
                payload=dict(result.detail),
                expires_at=self._clock() + self._detail_ttl,
            )
            note = self._store_summary_locked(identity, result)
        rec = self._record(
            identity, resource_id, VIEW_DETAIL, SOURCE_LOADER, True,
            f"loaded; detail cached per-principal (perm_version={result.perm_version}); "
            + note,
        )
        return dict(result.detail), rec

    # --------------------------------------------------------------- record

    def _record(self, identity, resource_id, view, source, ok, detail) -> ViewRecord:
        rec = ViewRecord(
            seq=next(self._seq),
            tenant=identity.tenant,
            principal=identity.principal,
            resource_id=resource_id,
            view=view,
            source=source,
            ok=ok,
            detail=detail,
        )
        with self._lock:
            self.records.append(rec)
        return rec
