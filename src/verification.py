"""Explicit-consent, offline-default credential verification boundary."""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

_ENDPOINTS = {
    "github": ("https://api.github.com/user", "token"),
    "stripe": ("https://api.stripe.com/v1/account", "bearer"),
}
_ALLOWED_DOMAINS = frozenset({"api.github.com", "api.stripe.com"})


@dataclass(frozen=True)
class VerificationOutcome:
    provider: str
    status: str
    checked_at: str
    attempts: int = 0
    http_status: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CredentialVerifier:
    def __init__(self, enabled: bool = False, timeout_seconds: int = 5, max_retries: int = 1, min_interval_seconds: float = 1.0):
        if type(enabled) is not bool:
            raise ValueError("enabled must be an exact boolean")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be an integer from 1 to 30")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be an integer from 0 to 2")
        if isinstance(min_interval_seconds, bool) or not isinstance(min_interval_seconds,(int,float)) or not 0 <= min_interval_seconds <= 3600:
            raise ValueError("min_interval_seconds is invalid")
        self.enabled=enabled; self.timeout_seconds=timeout_seconds
        self.max_retries=max_retries; self.min_interval_seconds=float(min_interval_seconds)
        self._last_request_at: float | None=None

    @staticmethod
    def _outcome(provider,status,attempts=0,http_status=None):
        return VerificationOutcome(provider=provider,status=status,checked_at=datetime.now(timezone.utc).isoformat(),attempts=attempts,http_status=http_status)

    def verify(self, provider: str, credential: str, consent: bool = False) -> VerificationOutcome:
        if type(consent) is not bool:raise ValueError("consent must be an exact boolean")
        if not self.enabled:return self._outcome(provider,"disabled")
        if consent is not True:return self._outcome(provider,"consent_required")
        if provider not in _ENDPOINTS:raise ValueError("Unsupported verification provider")
        if not isinstance(credential,str) or not credential or len(credential)>8192:raise ValueError("Credential is missing or exceeds the safe limit")
        now=time.monotonic()
        if self._last_request_at is not None and now-self._last_request_at < self.min_interval_seconds:
            return self._outcome(provider,"rate_limited")
        url,auth_kind=_ENDPOINTS[provider]
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!="https" or parsed.hostname not in _ALLOWED_DOMAINS or parsed.username or parsed.password:
            raise RuntimeError("Verification endpoint violated the fixed allowlist")
        prefix="token" if auth_kind=="token" else "Bearer"
        request=urllib.request.Request(url,headers={"Authorization":f"{prefix} {credential}","User-Agent":"CodeRiskTools-Scanner/4"},method="GET")
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        self._last_request_at=now
        deadline=now+self.timeout_seconds
        attempts=0
        while attempts <= self.max_retries:
            attempts+=1
            remaining=deadline-time.monotonic()
            if remaining<=0:return self._outcome(provider,"error",attempts-1)
            request_timeout=min(self.timeout_seconds,remaining)
            try:
                with opener.open(request,timeout=request_timeout) as response:
                    status=int(response.status)
                if 200 <= status < 300:return self._outcome(provider,"valid",attempts,status)
                if status in (400,401,403):return self._outcome(provider,"invalid",attempts,status)
                return self._outcome(provider,"error",attempts,status)
            except urllib.error.HTTPError as exc:
                if exc.code in (400,401,403):return self._outcome(provider,"invalid",attempts,exc.code)
                if attempts>self.max_retries:return self._outcome(provider,"error",attempts,exc.code)
            except (urllib.error.URLError,OSError,TimeoutError):
                if attempts>self.max_retries:return self._outcome(provider,"error",attempts)
            if attempts<=self.max_retries:
                remaining=deadline-time.monotonic()
                if remaining<=0:return self._outcome(provider,"error",attempts)
                delay=min(self.min_interval_seconds,remaining)
                if delay>0:time.sleep(delay)
                self._last_request_at=time.monotonic()
        return self._outcome(provider,"error",attempts)
