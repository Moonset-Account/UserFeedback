"""Controllable application adapters.

The adapter is the boundary between the logout service and the individual
applications (app A, app B, ...).  It is deliberately controllable so a
caller can:

* simulate an application timing out while it is asked to terminate
  sessions (``set_timeout`` / ``set_failures``),
* simulate a user logging in again on an application
  (``simulate_relogin``),
* inspect every terminate call the service made (``calls``).
"""


class AppTimeoutError(Exception):
    """Raised when an application fails to process a termination request."""


class ControllableAppAdapter:
    def __init__(self):
        self._timeout_apps = set()
        self._failures_remaining = {}
        self.calls = []

    # -- failure simulation -------------------------------------------------
    def set_timeout(self, app, enabled=True):
        """Make every terminate call for ``app`` time out (or not)."""
        if enabled:
            self._timeout_apps.add(app)
        else:
            self._timeout_apps.discard(app)

    def set_failures(self, app, count):
        """Make the next ``count`` terminate calls for ``app`` time out."""
        self._failures_remaining[app] = int(count)

    # -- service-facing interface -------------------------------------------
    def terminate(self, app, sessions):
        """Ask ``app`` to terminate ``sessions``.  May raise AppTimeoutError."""
        session_ids = [s.session_id for s in sessions]
        remaining = self._failures_remaining.get(app, 0)
        if app in self._timeout_apps or remaining > 0:
            if remaining > 0:
                self._failures_remaining[app] = remaining - 1
            self.calls.append(
                {"app": app, "session_ids": session_ids, "outcome": "timeout"}
            )
            raise AppTimeoutError("app %r timed out while terminating %s"
                                  % (app, session_ids))
        self.calls.append(
            {"app": app, "session_ids": session_ids, "outcome": "success"}
        )
        return session_ids

    # -- re-login simulation --------------------------------------------------
    def simulate_relogin(self, service, app, issuer, account, session_id,
                         login_time):
        """Record that a user established a fresh local session on ``app``."""
        return service.record_login(
            app=app,
            issuer=issuer,
            account=account,
            session_id=session_id,
            login_time=login_time,
        )
