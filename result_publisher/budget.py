"""Privacy quota ledgers, one per ``(dataset, approved purpose)`` scope."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict

from .errors import QuotaExceededError, UnknownScopeError

# Floating point slack so that an exactly-used-up budget is not reported
# as over-spent because of binary rounding (e.g. 0.1 + 0.2 vs 0.3).
_EPSILON = 1e-12


@dataclass(frozen=True)
class BudgetScope:
    """The authorization scope within which quota is granted."""

    dataset: str
    purpose: str

    def __str__(self) -> str:
        return f"{self.dataset}:{self.purpose}"


class Reservation:
    """A hold placed on a :class:`PrivacyBudget`.

    A reservation is a state machine::

        reserved --commit()--> committed
                 \\-release()--> released

    Only one terminal transition is ever applied; repeated calls are
    harmless. Released quota becomes available again; committed quota is
    permanently consumed.
    """

    def __init__(self, budget: "PrivacyBudget", amount: float) -> None:
        self._budget = budget
        self.amount = amount
        self._lock = threading.Lock()
        self._state = "reserved"

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == "reserved"

    def commit(self) -> bool:
        """Convert the hold into permanently consumed quota."""

        with self._lock:
            if self._state != "reserved":
                return False
            self._state = "committed"
            self._budget._on_commit(self)
            return True

    def release(self) -> bool:
        """Return the held quota to the available pool."""

        with self._lock:
            if self._state != "reserved":
                return False
            self._state = "released"
            self._budget._on_release(self)
            return True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Reservation(scope={self._budget.scope}, amount={self.amount}, "
            f"state={self._state})"
        )


class PrivacyBudget:
    """Thread-safe quota ledger for a single authorization scope."""

    def __init__(self, scope: BudgetScope, limit: float) -> None:
        if limit < 0:
            raise ValueError("budget limit must be non-negative")
        self.scope = scope
        self.limit = float(limit)
        self._lock = threading.Lock()
        self._consumed = 0.0
        self._reserved = 0.0

    @property
    def consumed(self) -> float:
        with self._lock:
            return self._consumed

    @property
    def reserved(self) -> float:
        with self._lock:
            return self._reserved

    @property
    def available(self) -> float:
        """Quota still spendable: limit minus consumed and open holds."""

        with self._lock:
            return max(0.0, self.limit - self._consumed - self._reserved)

    def reserve(self, amount: float) -> Reservation:
        """Reserve ``amount`` or raise :class:`QuotaExceededError`."""

        if amount < 0:
            raise ValueError("reservation amount must be non-negative")
        with self._lock:
            available = self.limit - self._consumed - self._reserved
            if amount > available + _EPSILON:
                raise QuotaExceededError(
                    str(self.scope), amount, max(0.0, available)
                )
            self._reserved += amount
            return Reservation(self, amount)

    def _on_commit(self, reservation: Reservation) -> None:
        with self._lock:
            self._reserved -= reservation.amount
            self._consumed += reservation.amount

    def _on_release(self, reservation: Reservation) -> None:
        with self._lock:
            self._reserved -= reservation.amount


class BudgetRegistry:
    """Holds the privacy budgets granted for every authorization scope."""

    def __init__(self, grants: Dict[BudgetScope, float] | None = None) -> None:
        self._budgets: Dict[BudgetScope, PrivacyBudget] = {}
        for scope, limit in (grants or {}).items():
            self.grant(scope, limit)

    def grant(self, scope: BudgetScope, limit: float) -> PrivacyBudget:
        """Register (or replace) the budget for a scope."""

        if not isinstance(scope, BudgetScope):
            raise TypeError("scope must be a BudgetScope")
        budget = PrivacyBudget(scope, limit)
        self._budgets[scope] = budget
        return budget

    def get(self, scope: BudgetScope) -> PrivacyBudget:
        try:
            return self._budgets[scope]
        except KeyError:
            raise UnknownScopeError(str(scope)) from None

    def __contains__(self, scope: object) -> bool:
        return scope in self._budgets

    def scopes(self) -> tuple[BudgetScope, ...]:
        return tuple(self._budgets)
