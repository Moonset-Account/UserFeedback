"""Deterministic synthetic data used by the test-suite and manual checks.

Nothing here is derived from real data; subject ids are simple opaque tokens.
Two scenarios are provided:

* :func:`build_interleaving_scenario` -- the central case: two releases whose
  previews both pass at the same base version, but publishing the first makes
  the second collapse into singleton equivalence classes.
* :func:`build_missing_value_scenario` -- historical absence is a visible value
  shared by all missing subjects until one subject becomes uniquely missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import ReviewService
from .model import Member, PublishedView, PublishRequest


# Three analysis groups with overlapping membership:
#   alice -> g1 only, bob -> g2 only, carol -> g1+g2 (the overlap), dave -> g3
MEMBERS = [
    Member("alice", ("g1",)),
    Member("bob", ("g2",)),
    Member("carol", ("g1", "g2")),
    Member("dave", ("g3",)),
]


@dataclass(frozen=True)
class InterleavingScenario:
    service: ReviewService
    historical: tuple[PublishedView, ...]
    first_request: PublishRequest
    second_request: PublishRequest


def build_interleaving_scenario() -> InterleavingScenario:
    """Four subjects, k=2, one coarse history and two complementary splits.

    History (v1 for g1, v2 for g2): all four subjects look identical
    (``hx='H'``), so every equivalence class has size 4 on its own.

    ``first_request`` splits the four subjects into two pairs; ``second_request``
    splits them along the *other* pairing. Each release alone keeps class size
    2 (>= k), but together the cross product leaves every class at size 1.
    """
    subjects = ("s1", "s2", "s3", "s4")
    history_records = {s: {"hx": "H"} for s in subjects}
    v1 = PublishedView(1, "g1", ("hx",), history_records)
    v2 = PublishedView(2, "g2", ("hx",), history_records)

    service = ReviewService(MEMBERS, k=2)
    service.add_published_view(v1)
    service.add_published_view(v2)

    first_request = PublishRequest(
        request_id="req-b1",
        columns=("nx",),
        records={
            "s1": {"nx": "A"},
            "s2": {"nx": "A"},
            "s3": {"nx": "B"},
            "s4": {"nx": "B"},
        },
        audience_groups=("g1", "g2"),
    )
    second_request = PublishRequest(
        request_id="req-b2",
        columns=("ny",),
        records={
            "s1": {"ny": "P"},
            "s3": {"ny": "P"},
            "s2": {"ny": "Q"},
            "s4": {"ny": "Q"},
        },
        audience_groups=("g1", "g2"),
    )
    return InterleavingScenario(
        service=service,
        historical=(v1, v2),
        first_request=first_request,
        second_request=second_request,
    )


@dataclass(frozen=True)
class MissingValueScenario:
    request: PublishRequest
    rejecting_history: tuple[PublishedView, ...]
    accepting_history: tuple[PublishedView, ...]


def build_missing_value_scenario() -> MissingValueScenario:
    """Four subjects; the historical view covers only some of them.

    Rejecting history: three covered subjects share ``H`` while one subject is
    absent, forming a unique MISSING class of size 1 (k=2) -> reject.

    Accepting history: two covered subjects share ``H`` and the other two are
    jointly absent, forming a MISSING class of size 2 -> both classes have
    size 2, so the release is accepted. This proves absence is grouped as an
    ordinary visible value rather than dropping the subjects from the check.
    """
    request = PublishRequest(
        request_id="req-missing",
        columns=("nz",),
        records={
            "s1": {"nz": "X"},
            "s2": {"nz": "X"},
            "s3": {"nz": "X"},
            "s4": {"nz": "X"},
        },
        audience_groups=("g1",),
    )

    rejecting_history = (
        PublishedView(
            1,
            "g1",
            ("hx",),
            {
                "s1": {"hx": "H"},
                "s2": {"hx": "H"},
                "s3": {"hx": "H"},
                # s4 absent -> unique MISSING class of size 1
            },
        ),
    )
    accepting_history = (
        PublishedView(
            1,
            "g1",
            ("hx",),
            {
                "s1": {"hx": "H"},
                "s2": {"hx": "H"},
                # s3 and s4 absent -> shared MISSING class of size 2
            },
        ),
    )

    return MissingValueScenario(
        request=request,
        rejecting_history=rejecting_history,
        accepting_history=accepting_history,
    )
