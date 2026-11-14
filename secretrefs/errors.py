"""Error types shared by the resolver."""

from __future__ import annotations


class InputError(Exception):
    """Raised when the deployment inputs themselves cannot be understood.

    This is distinct from a *resolution* failure: unreadable or malformed
    input aborts the run, while an unresolvable but well-formed reference only
    makes the owning service's injection plan non-executable.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def as_dict(self) -> dict:
        data = {"message": self.message}
        if self.path is not None:
            data["path"] = self.path
        return data
