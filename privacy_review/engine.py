"""Privacy review engine: k-anonymity previews and version-bound publishing.

The engine is fully local, deterministic and only operates on synthetic data.
It implements three non-obvious rules from the task:

1. A member's *combined visible row* joins, by internal subject id, every
   column they could see in already published views with the columns of the
   proposed release. A subject missing from a historical view contributes the
   :data:`~privacy_review.model.MISSING` sentinel, so absence itself is a
   visible value and a grouping key.

2. A preview is bound to the version of published views current at preview
   time. Publishing an accepted request creates a new view; a later publish
   referencing an older preview is re-evaluated against the newest version
   (``recomputed=True``) and may be rejected even though its preview passed.

3. Re-submitting the same ``request_id`` returns the stored outcome without
   re-computation; submitting the same id with a different payload is an error.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .model import (
    Decision,
    MISSING,
    Member,
    PublishedView,
    PublishRequest,
    ReviewResult,
    Risk,
)


class ReviewService:
    """Stateful service holding members, published views and review outcomes."""

    def __init__(self, members: Iterable[Member], k: int) -> None:
        members = list(members)
        if not members:
            raise ValueError("at least one member is required")
        if not isinstance(k, int) or k < 1:
            raise ValueError("k must be a positive integer")
        self._members: list[Member] = members
        self._k = k
        self._views: list[PublishedView] = []
        self._previews: dict[str, ReviewResult] = {}
        self._publishes: dict[str, ReviewResult] = {}
        self._payloads: dict[str, PublishRequest] = {}

    # ------------------------------------------------------------------ state
    @property
    def k(self) -> int:
        return self._k

    @property
    def current_version(self) -> int:
        """Version number of the newest published view (0 when none)."""
        return self._views[-1].version if self._views else 0

    @property
    def published_views(self) -> tuple[PublishedView, ...]:
        return tuple(self._views)

    def add_published_view(self, view: PublishedView) -> None:
        """Seed the service with an already released view."""
        if self._views and view.version != self.current_version + 1:
            raise ValueError(
                f"expected version {self.current_version + 1}, got {view.version}"
            )
        if not self._views and view.version != 1:
            raise ValueError("first published view must have version 1")
        self._views.append(view)

    # --------------------------------------------------------------- preview
    def preview(self, request: PublishRequest) -> ReviewResult:
        """Evaluate a request against the current views, without changing them.

        Repeated calls with the same ``request_id`` return the stored result,
        even if further views have since been published: the first preview
        stays bound to its base version.
        """
        self._remember_payload(request)
        cached = self._previews.get(request.request_id)
        if cached is not None:
            return cached
        result = self._evaluate(request, base_version=self.current_version, recomputed=False)
        self._previews[request.request_id] = result
        return result

    # --------------------------------------------------------------- publish
    def publish(
        self,
        request: PublishRequest,
        preview: ReviewResult | None = None,
    ) -> ReviewResult:
        """Publish a request, optionally referencing a prior preview.

        * Without a preview the request is evaluated against the current
          version.
        * With a preview whose base version is still current, the preview's
          decision stands (no re-computation).
        * With a stale preview the request is re-evaluated against the newest
          version; a request that passed alone can therefore be rejected once
          combined with concurrently published columns.

        An accepted request is materialised as one new
        :class:`PublishedView` per audience group at the next version.
        Rejected requests create no view. Re-submitting the same
        ``request_id`` returns the first stored outcome verbatim.
        """
        self._remember_payload(request)
        cached = self._publishes.get(request.request_id)
        if cached is not None:
            return cached

        if preview is None:
            result = self._evaluate(
                request, base_version=self.current_version, recomputed=False
            )
        else:
            if preview.request_id != request.request_id:
                raise ValueError("preview belongs to a different request_id")
            if preview.base_version == self.current_version:
                # Preview is still current: its decision is authoritative.
                result = self._clone_as_publish(preview)
            else:
                # Views advanced since the preview: recompute against latest.
                result = self._evaluate(
                    request,
                    base_version=self.current_version,
                    recomputed=True,
                )

        self._publishes[request.request_id] = result
        if result.accepted:
            next_version = self.current_version + 1
            for group_id in request.audience_groups:
                self._views.append(
                    PublishedView(
                        version=next_version,
                        group_id=group_id,
                        columns=request.columns,
                        records=request.records,
                    )
                )
        return result

    # ------------------------------------------------------------- internals
    def _remember_payload(self, request: PublishRequest) -> None:
        previous = self._payloads.get(request.request_id)
        if previous is None:
            self._payloads[request.request_id] = request
            return
        if previous != request:
            raise ValueError(
                f"request_id {request.request_id!r} was already submitted "
                "with a different payload"
            )

    def _clone_as_publish(self, preview: ReviewResult) -> ReviewResult:
        return ReviewResult(
            request_id=preview.request_id,
            decision=preview.decision,
            base_version=preview.base_version,
            k=preview.k,
            risks=preview.risks,
            minimum_group_size=preview.minimum_group_size,
            recomputed=preview.recomputed,
            published_version=(
                preview.base_version + 1 if preview.accepted else None
            ),
        )

    def _reachable_combinations(
        self, audience_groups: Sequence[str]
    ) -> set[tuple[str, ...]]:
        audience = set(audience_groups)
        combos: set[tuple[str, ...]] = set()
        for member in self._members:
            if set(member.groups) & audience:
                combos.add(member.combination)
        return combos

    def _history_for(
        self,
        combination: tuple[str, ...],
        subjects: Sequence[str],
    ) -> tuple[list[str], dict[str, tuple[Any, ...]]]:
        """Build historical visible rows for members of one group combination.

        Columns are taken from every published view whose group is part of the
        combination (a member sees the union of their groups' views). Missing
        columns and subjects absent from a view both resolve to MISSING.
        """
        visible_views = [v for v in self._views if v.group_id in set(combination)]
        columns: list[str] = []
        for view in visible_views:
            for column in view.columns:
                if column not in columns:
                    columns.append(column)

        rows: dict[str, tuple[Any, ...]] = {}
        for subject in subjects:
            row: list[Any] = []
            for column in columns:
                value: Any = MISSING
                for view in visible_views:
                    if column in view.columns:
                        source = view.records.get(subject)
                        value = MISSING if source is None else source.get(column, MISSING)
                        break
                row.append(value)
            rows[subject] = tuple(row)
        return columns, rows

    def _evaluate(
        self,
        request: PublishRequest,
        base_version: int,
        recomputed: bool,
    ) -> ReviewResult:
        unknown = set(request.audience_groups) - {
            group for member in self._members for group in member.groups
        }
        if unknown:
            raise ValueError(f"unknown audience groups: {sorted(unknown)}")

        combinations = self._reachable_combinations(request.audience_groups)
        subjects = tuple(request.records)
        risks: list[Risk] = []
        overall_minimum: int | None = None

        for combination in sorted(combinations):
            history_columns, history_rows = self._history_for(combination, subjects)
            overlap = set(history_columns) & set(request.columns)
            if overlap:
                raise ValueError(
                    "new columns must not shadow already visible columns: "
                    f"{sorted(overlap)}"
                )

            # Equivalence-class key: all historical visible values (MISSING
            # included) followed by the proposed release's values.
            buckets: dict[tuple[Any, ...], set[str]] = {}
            for subject in subjects:
                key = history_rows[subject] + tuple(
                    request.records[subject][column] for column in request.columns
                )
                buckets.setdefault(key, set()).add(subject)

            minimum = min((len(members_) for members_ in buckets.values()), default=0)
            overall_minimum = minimum if overall_minimum is None else min(overall_minimum, minimum)
            if minimum < self._k:
                risks.append(
                    Risk(
                        audience_combination=combination,
                        minimum_group_size=minimum,
                    )
                )

        decision = Decision.REJECT if risks else Decision.ACCEPT
        return ReviewResult(
            request_id=request.request_id,
            decision=decision,
            base_version=base_version,
            k=self._k,
            risks=tuple(risks),
            minimum_group_size=overall_minimum,
            recomputed=recomputed,
            published_version=(base_version + 1 if decision is Decision.ACCEPT else None),
        )
