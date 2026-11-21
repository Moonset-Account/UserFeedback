"""Exceptions raised by the shuffle scheduler."""

from __future__ import annotations


class SchedulerError(Exception):
    """Base class for all scheduler errors."""


class UnknownPartition(SchedulerError):
    def __init__(self, pid: str) -> None:
        super().__init__(f"unknown partition: {pid!r}")
        self.pid = pid


class DuplicatePartition(SchedulerError):
    def __init__(self, pid: str) -> None:
        super().__init__(f"partition declared twice: {pid!r}")
        self.pid = pid


class UnknownDependency(SchedulerError):
    def __init__(self, pid: str, upstream: str) -> None:
        super().__init__(f"partition {pid!r} depends on unknown partition {upstream!r}")
        self.pid = pid
        self.upstream = upstream


class DuplicateAttempt(SchedulerError):
    def __init__(self, attempt_id: str) -> None:
        super().__init__(f"attempt id already used: {attempt_id!r}")
        self.attempt_id = attempt_id


class UnknownAttempt(SchedulerError):
    def __init__(self, attempt_id: str) -> None:
        super().__init__(f"no in-flight attempt with id {attempt_id!r}")
        self.attempt_id = attempt_id


class InvalidReadSet(SchedulerError):
    """A result reported reading something it was not allowed to read."""

    def __init__(self, pid: str, upstream: str, detail: str) -> None:
        super().__init__(f"partition {pid!r} read {upstream!r}: {detail}")
        self.pid = pid
        self.upstream = upstream


__all__ = [
    "SchedulerError",
    "UnknownPartition",
    "DuplicatePartition",
    "UnknownDependency",
    "DuplicateAttempt",
    "UnknownAttempt",
    "InvalidReadSet",
]
