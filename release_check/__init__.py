"""Static release checker for a single shared profile-generation method.

The library answers one question: given a finite set of caller identities, an
interface inheritance graph with per-method visibility declarations, and a set
of candidate implementations, may each implementation be released in place of
the current one?

Contract of a callable entry ``(interface, method)``:

* the set of identities admitted through the entry, and
* for each admitted identity, the maximum fields it may observe.

An implementation is acceptable when every call that is legal through a public
interface still succeeds against it and returns only fields inside the range
the entry grants that identity.  Diamond inheritance is handled by making all
inherited branch contracts bind at once.  An entry whose public ancestor
exposes the method without any declaration stays *undecided* instead of being
guessed.

Typical use::

    from release_check import parse_spec, check, render_text

    spec = parse_spec(document)
    report = check(spec)
    print(render_text(report))
    report.status  # "ready" | "pending" | "rejected"
"""

from __future__ import annotations

from .checker import (
    Finding,
    PendingEntry,
    Report,
    check,
    check_substitution,
)
from .model import (
    InputError,
    Interface,
    Implementation,
    ImplMethod,
    Resolution,
    Spec,
    load_document,
    parse_spec,
)
from .report import render_json, render_text

__all__ = [
    "Finding",
    "ImplMethod",
    "Implementation",
    "InputError",
    "Interface",
    "PendingEntry",
    "Report",
    "Resolution",
    "Spec",
    "check",
    "check_substitution",
    "load_document",
    "parse_spec",
    "render_json",
    "render_text",
]

__version__ = "1.0.0"
