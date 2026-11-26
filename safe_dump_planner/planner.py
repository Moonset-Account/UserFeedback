"""Core safe dump planning algorithm."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import ValidationError
from .geometry import (
    ByteRange,
    complement_ranges,
    intersect_unions,
    normalize_ranges,
    range_covers,
    uncovered_parts,
)
from .model import (
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    FragmentNeed,
    PageSpec,
    PointerRef,
    VirtualAlias,
)
from .report import (
    DumpPlan,
    Fragment,
    PagePlan,
    PointerDecision,
    Reason,
    TargetDecision,
)


DEFAULT_POLICY_DOMAIN = "*default*"


@dataclass
class Atom:
    page_id: str
    start: int
    end: int

    @property
    def range(self) -> ByteRange:
        return ByteRange(self.start, self.end)

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def atom_id(self) -> str:
        return f"{self.page_id}:{self.start}:{self.end}"


@dataclass
class ResolvedNeed:
    need: FragmentNeed
    page_id: str | None
    ranges: tuple[ByteRange, ...]
    atom_ids: tuple[str, ...] = ()
    feasible: bool = False
    reasons: tuple[Reason, ...] = ()
    direct_bytes: int = 0


@dataclass
class PointerInfo:
    index: int
    spec: PointerRef
    key: str
    page_id: str
    range: ByteRange
    target_page: str | None
    resolved: bool
    value_releasable: bool = False


@dataclass
class Evaluation:
    seed_ids: tuple[str, ...]
    atom_ids: set[str]
    cost: int
    within_budget: bool
    hard_failure: bool
    retained_by_page: dict[str, tuple[ByteRange, ...]]
    masked_by_page: dict[str, tuple[ByteRange, ...]]
    supported_ids: tuple[str, ...]
    complete_ids: tuple[str, ...]
    redacted_ids: tuple[str, ...]
    covered_ids: tuple[str, ...]
    objective: tuple[int, ...]
    signature: tuple[tuple[str, int, int], ...]
    closure_reasons: tuple[Reason, ...] = ()
    pointer_status: dict[str, str] = field(default_factory=dict)


def plan_safe_dump(request: DumpRequest | dict[str, Any] | None = None, **kwargs: Any) -> DumpPlan:
    """Plan a safe dump.

    The first argument may be a :class:`DumpRequest`, a JSON-friendly dict, or
    omitted so that request fields are passed as keywords.
    """

    if request is None:
        req = DumpRequest(**kwargs)
    elif isinstance(request, dict):
        req = DumpRequest.from_dict(request)
    elif isinstance(request, DumpRequest):
        req = request
    else:
        raise TypeError("request must be a DumpRequest, dict, or None")
    return Planner(req).plan()


# Concise alias for callers that prefer the problem-domain name.
plan_dump = plan_safe_dump


class Planner:
    def __init__(self, request: DumpRequest) -> None:
        self.req = request
        self.pages: dict[str, PageSpec] = {}
        self.page_order: list[str] = []
        self.aliases: dict[str, VirtualAlias] = {}
        self.targets: dict[str, DiagnosticTarget] = {}
        self.target_order: list[str] = []
        self.field_map: dict[str, dict[str, ByteRange]] = {}
        self.bare_fields: dict[str, dict[str, list[str]]] = {}
        self.record_map: dict[str, dict[str, ByteRange]] = {}
        self.handle: dict[str, str] = {}

        self.effective_domains: dict[str, tuple[str, ...]] = {}
        self.domain_allowed: dict[str, dict[str, tuple[ByteRange, ...]]] = {}
        self.allowed: dict[str, tuple[ByteRange, ...]] = {}
        self.address_allowed: dict[str, bool] = {}
        self.crop_ranges: dict[str, tuple[ByteRange, ...]] = {}
        self.secret_ranges: dict[str, list[ByteRange]] = {}

        self.pointer_infos: list[PointerInfo] = []
        self.atoms: dict[str, list[Atom]] = {}
        self.atom_by_id: dict[str, Atom] = {}
        self.need_cache: dict[int, ResolvedNeed] = {}
        self.target_needs: dict[str, list[ResolvedNeed]] = {}
        self.target_assessment: dict[str, ResolvedNeed] = {}
        self.search_info: dict[str, Any] = {}

        self.priority_levels: list[int] = []

    # ------------------------------------------------------------------ setup

    def plan(self) -> DumpPlan:
        self._index_inputs()
        self._build_permissions()
        self._build_pointer_infos()
        self._build_secret_ranges()
        self._resolve_all_needs_before_atoms()
        self._build_atoms()
        self._assess_all_needs()
        best, search_info = self._search()
        return self._build_plan(best, search_info)

    def _index_inputs(self) -> None:
        for index, page in enumerate(self.req.pages):
            if page.page_id in self.pages:
                raise ValidationError(f"duplicate page_id {page.page_id!r}")
            self.pages[page.page_id] = page
            self.page_order.append(page.page_id)
            self.handle[page.page_id] = f"page-{index + 1:04d}"
            self.field_map[page.page_id] = {}
            self.bare_fields[page.page_id] = {}
            self.record_map[page.page_id] = {}
            self._index_fields(page.page_id, page.fields)
            for record in page.records:
                if record.record_id in self.record_map[page.page_id]:
                    raise ValidationError(
                        f"duplicate record_id {record.record_id!r} on page {page.page_id!r}"
                    )
                self.record_map[page.page_id][record.record_id] = record.range
                self._index_fields(page.page_id, record.fields, record.record_id)

        for alias in self.req.aliases:
            if alias.alias_id in self.aliases:
                raise ValidationError(f"duplicate alias_id {alias.alias_id!r}")
            if alias.page_id not in self.pages:
                raise ValidationError(
                    f"alias {alias.alias_id!r} refers to unknown page {alias.page_id!r}"
                )
            self.aliases[alias.alias_id] = alias

        for target in self.req.targets:
            if target.target_id in self.targets:
                raise ValidationError(f"duplicate target_id {target.target_id!r}")
            self.targets[target.target_id] = target
            self.target_order.append(target.target_id)

        self.priority_levels = sorted(
            {t.priority for t in self.req.targets}, reverse=True
        )

    def _index_fields(
        self,
        page_id: str,
        fields: Iterable[Any],
        record_id: str | None = None,
    ) -> None:
        for f in fields:
            qualified = f"{record_id}.{f.name}" if record_id else f.name
            self.field_map[page_id][qualified] = f.range
            if not record_id:
                # A top-level field is also available by its bare name.
                self.field_map[page_id][f.name] = f.range
            self.bare_fields[page_id].setdefault(f.name, []).append(qualified)

    def _resolve_page_ref(
        self,
        page_id: str | None = None,
        alias_id: str | None = None,
    ) -> tuple[str, VirtualAlias | None]:
        if alias_id is not None:
            if alias_id not in self.aliases:
                raise ValidationError(f"unknown alias_id {alias_id!r}")
            alias = self.aliases[alias_id]
            return alias.page_id, alias
        if page_id is not None:
            if page_id not in self.pages:
                raise ValidationError(f"unknown page_id {page_id!r}")
            return page_id, None
        raise ValidationError("either page_id or alias_id is required")

    def _resolve_field_range(self, page_id: str, name: str) -> ByteRange:
        if name in self.field_map[page_id]:
            return self.field_map[page_id][name]
        candidates = self.bare_fields[page_id].get(name, [])
        if len(candidates) == 1:
            return self.field_map[page_id][candidates[0]]
        if len(candidates) > 1:
            raise ValidationError(
                f"field name {name!r} is ambiguous on page {page_id!r}; "
                f"use one of {', '.join(candidates)}"
            )
        raise ValidationError(f"unknown field {name!r} on page {page_id!r}")

    # ------------------------------------------------------------- permissions

    def _build_permissions(self) -> None:
        policies_by_domain: dict[str, list[DomainPolicy]] = {}
        for policy in self.req.policies:
            policies_by_domain.setdefault(policy.domain, []).append(policy)
        wildcard = policies_by_domain.get("*", [])

        for pid in self.page_order:
            page = self.pages[pid]
            domains: set[str] = set(page.coherent_domains)
            if page.owner_domain:
                domains.add(page.owner_domain)
            for alias in self.req.aliases:
                if alias.page_id == pid and alias.domain:
                    domains.add(alias.domain)

            ordered = tuple(sorted(domains))
            if not ordered and self.req.default_policy is not None:
                effective = (DEFAULT_POLICY_DOMAIN,)
                policies = [self.req.default_policy]
            else:
                effective = ordered
                policies = []

            self.effective_domains[pid] = effective
            domain_ranges: dict[str, tuple[ByteRange, ...]] = {}
            address_map: dict[str, bool] = {}

            if not ordered and self.req.default_policy is not None:
                allowed, addr = self._combine_policy_permissions(pid, policies)
                domain_ranges[DEFAULT_POLICY_DOMAIN] = allowed
                address_map[DEFAULT_POLICY_DOMAIN] = addr
            else:
                for domain in ordered:
                    domain_policies = policies_by_domain.get(domain)
                    if domain_policies is None:
                        domain_policies = wildcard
                    allowed, addr = self._combine_policy_permissions(
                        pid, domain_policies or []
                    )
                    domain_ranges[domain] = allowed
                    address_map[domain] = addr

            self.domain_allowed[pid] = domain_ranges
            common: tuple[ByteRange, ...] = (ByteRange(0, page.size),)
            if not effective:
                common = ()
            else:
                for domain in effective:
                    common = intersect_unions(common, domain_ranges.get(domain, ()))
            self.allowed[pid] = normalize_ranges(common, page_size=page.size)
            self.address_allowed[pid] = bool(effective) and all(
                address_map.get(d, False) for d in effective
            )
            self.crop_ranges[pid] = self._build_crop_ranges(pid)

    def _combine_policy_permissions(
        self,
        page_id: str,
        policies: list[DomainPolicy],
    ) -> tuple[tuple[ByteRange, ...], bool]:
        page = self.pages[page_id]
        allows: list[ByteRange] = []
        denies: list[ByteRange] = []
        address_allow = False
        address_deny = False

        for policy in policies:
            if policy.allow_all_content:
                allows.append(ByteRange(0, page.size))
            if policy.allow_all_addresses:
                address_allow = True
            if "*" in policy.allow_address_pages or page_id in policy.allow_address_pages:
                address_allow = True
            if "*" in policy.deny_address_pages or page_id in policy.deny_address_pages:
                address_deny = True

            for rule in policy.allow:
                if rule.page_id not in ("*", page_id):
                    continue
                if rule.whole_page:
                    allows.append(ByteRange(0, page.size))
                allows.extend(rule.ranges)
                for name in rule.field_names:
                    allows.append(self._resolve_field_range(page_id, name))

            for rule in policy.deny:
                if rule.page_id not in ("*", page_id):
                    continue
                if rule.whole_page:
                    denies.append(ByteRange(0, page.size))
                denies.extend(rule.ranges)
                for name in rule.field_names:
                    denies.append(self._resolve_field_range(page_id, name))

        allowed_union = normalize_ranges(allows, page_size=page.size)
        denied_union = normalize_ranges(denies, page_size=page.size)
        allowed = uncovered_parts(
            (ByteRange(0, page.size),) if False else allowed_union,
            denied_union,
        )
        # uncovered_parts is source - removed and works on arbitrary normalized sets.
        address = address_allow and not address_deny
        return normalize_ranges(allowed, page_size=page.size), address

    def _build_crop_ranges(self, page_id: str) -> tuple[ByteRange, ...]:
        if not self.req.crops:
            return (ByteRange(0, self.pages[page_id].size),)
        out: list[ByteRange] = []
        for crop in self.req.crops:
            try:
                cpid, _ = self._resolve_page_ref(crop.page_id, crop.alias_id)
            except ValidationError:
                if self.req.strict:
                    raise
                continue
            if cpid != page_id:
                continue
            if crop.whole_page:
                out.append(ByteRange(0, self.pages[page_id].size))
            out.extend(crop.ranges)
            for name in crop.field_names:
                out.append(self._resolve_field_range(page_id, name))
            for rid in crop.record_ids:
                if rid not in self.record_map[page_id]:
                    raise ValidationError(
                        f"crop refers to unknown record_id {rid!r} on page {page_id!r}"
                    )
                out.append(self.record_map[page_id][rid])
        return normalize_ranges(out, page_size=self.pages[page_id].size)

    # --------------------------------------------------------------- pointers

    def _build_pointer_infos(self) -> None:
        for i, spec in enumerate(self.req.pointers):
            pid, alias = self._resolve_page_ref(spec.page_id, spec.alias_id)
            page = self.pages[pid]
            pr = spec.range
            if pr.end > page.size:
                raise ValidationError(
                    f"pointer {spec.pointer_id or spec.label or i} range {pr} "
                    f"is outside page {pid!r} of size {page.size}"
                )
            if spec.domain:
                self._add_coherent_domain(pid, spec.domain)

            target_page: str | None = None
            resolved = False
            if spec.target_alias_id is not None:
                if spec.target_alias_id not in self.aliases:
                    raise ValidationError(
                        f"pointer {i} refers to unknown target alias "
                        f"{spec.target_alias_id!r}"
                    )
                target_page = self.aliases[spec.target_alias_id].page_id
                resolved = True
            elif spec.target_page is not None:
                if spec.target_page not in self.pages:
                    raise ValidationError(
                        f"pointer {i} refers to unknown target page {spec.target_page!r}"
                    )
                target_page = spec.target_page
                resolved = True
            elif spec.target_virtual_address is not None:
                candidates = [
                    a
                    for a in self.req.aliases
                    if a.virtual_address == spec.target_virtual_address
                ]
                if spec.domain is not None:
                    domain_matches = [a for a in candidates if a.domain == spec.domain]
                    if domain_matches:
                        candidates = domain_matches
                page_ids = {a.page_id for a in candidates}
                if len(page_ids) == 1:
                    target_page = next(iter(page_ids))
                    resolved = True
                elif len(page_ids) > 1 and self.req.strict:
                    raise ValidationError(
                        f"pointer {i} virtual address "
                        f"{spec.target_virtual_address:#x} is ambiguous across pages "
                        f"{sorted(page_ids)}; set pointer.domain or target_page"
                    )

            if target_page is not None and spec.domain:
                self._add_coherent_domain(target_page, spec.domain)

            if resolved and target_page is not None:
                value_releasable = self.address_allowed.get(target_page, False)
            else:
                value_releasable = not self.req.mask_unresolved_pointers

            key = spec.pointer_id or spec.label or f"ptr-{i + 1}"
            self.pointer_infos.append(
                PointerInfo(
                    index=i,
                    spec=spec,
                    key=key,
                    page_id=pid,
                    range=pr,
                    target_page=target_page,
                    resolved=resolved,
                    value_releasable=value_releasable,
                )
            )

    def _add_coherent_domain(self, page_id: str, domain: str) -> None:
        if domain == DEFAULT_POLICY_DOMAIN:
            return
        domains = set(self.effective_domains.get(page_id, ()))
        if DEFAULT_POLICY_DOMAIN in domains:
            domains.discard(DEFAULT_POLICY_DOMAIN)
        if domain not in domains:
            domains.add(domain)
            self.effective_domains[page_id] = tuple(sorted(domains))
            self._recompute_target_permission(page_id)

    def _recompute_target_permission(self, page_id: str) -> None:
        """Recompute intersection after discovering a pointer domain."""

        policies_by_domain: dict[str, list[DomainPolicy]] = {}
        for policy in self.req.policies:
            policies_by_domain.setdefault(policy.domain, []).append(policy)
        wildcard = policies_by_domain.get("*", [])

        domains = set(self.effective_domains.get(page_id, ()))
        # Remove synthetic default marker if real domains were discovered.
        domains.discard(DEFAULT_POLICY_DOMAIN)
        ordered = tuple(sorted(domains))
        self.effective_domains[page_id] = ordered
        domain_ranges: dict[str, tuple[ByteRange, ...]] = {}
        address_map: dict[str, bool] = {}
        for domain in ordered:
            pols = policies_by_domain.get(domain) or wildcard
            allowed, addr = self._combine_policy_permissions(page_id, pols)
            domain_ranges[domain] = allowed
            address_map[domain] = addr
        self.domain_allowed[page_id] = domain_ranges
        common: tuple[ByteRange, ...] = (ByteRange(0, self.pages[page_id].size),)
        for domain in ordered:
            common = intersect_unions(common, domain_ranges.get(domain, ()))
        self.allowed[page_id] = normalize_ranges(
            common, page_size=self.pages[page_id].size
        )
        self.address_allowed[page_id] = bool(ordered) and all(
            address_map.get(d, False) for d in ordered
        )

    # ------------------------------------------------------------- secrets

    def _build_secret_ranges(self) -> None:
        # Addresses are global secrets: a restricted target address can appear
        # as a pointer or as arbitrary bytes on any source page.
        restricted_addresses: set[int] = set()
        for pid, page in self.pages.items():
            if self.address_allowed.get(pid, False):
                continue
            if page.physical_address is not None and page.physical_address != 0:
                restricted_addresses.add(page.physical_address)
            for alias in self.req.aliases:
                if alias.page_id == pid and alias.virtual_address not in (None, 0):
                    restricted_addresses.add(int(alias.virtual_address))
            for pinfo in self.pointer_infos:
                if pinfo.target_page == pid:
                    if pinfo.spec.value not in (None, 0):
                        restricted_addresses.add(int(pinfo.spec.value))
                    if pinfo.spec.target_virtual_address not in (None, 0):
                        restricted_addresses.add(int(pinfo.spec.target_virtual_address))

        for pid, page in self.pages.items():
            ranges: list[ByteRange] = []
            data = page.padded_data
            for address in restricted_addresses:
                patterns: list[bytes] = []
                for width in (8, 4):
                    if address < (1 << (8 * width)):
                        patterns.append(int(address).to_bytes(width, "little"))
                        patterns.append(int(address).to_bytes(width, "big"))
                if self.req.scan_text_addresses:
                    patterns.append(hex(address).encode("ascii"))
                for pattern in patterns:
                    if not pattern:
                        continue
                    start = data.find(pattern)
                    while start != -1:
                        end = min(page.size, start + len(pattern))
                        if end > start:
                            ranges.append(ByteRange(start, end))
                        start = data.find(pattern, start + 1)
            self.secret_ranges[pid] = list(
                normalize_ranges(ranges, page_size=page.size)
            )

    # --------------------------------------------------------------- needs

    def _resolve_all_needs_before_atoms(self) -> None:
        for tid, target in self.targets.items():
            resolved: list[ResolvedNeed] = []
            for need in target.all_needs():
                resolved.append(self._resolve_need_intervals(need))
            self.target_needs[tid] = resolved

        for pinfo in self.pointer_infos:
            if pinfo.spec.downstream is not None:
                key = id(pinfo.spec.downstream)
                if key not in self.need_cache:
                    self.need_cache[key] = self._resolve_need_intervals(
                        pinfo.spec.downstream
                    )

    def _resolve_need_intervals(self, need: FragmentNeed) -> ResolvedNeed:
        try:
            pid, _ = self._resolve_page_ref(need.page_id, need.alias_id)
            intervals: list[ByteRange] = list(need.ranges)
            if need.record_id is not None:
                if pid is None or need.record_id not in self.record_map[pid]:
                    raise ValidationError(
                        f"unknown record_id {need.record_id!r} for page {need.page_id!r}"
                    )
                record_range = self.record_map[pid][need.record_id]
                if not need.field_names and not need.ranges:
                    intervals.append(record_range)
            if pid is not None:
                for name in need.field_names:
                    intervals.append(self._resolve_field_range(pid, name))
                ranges = normalize_ranges(
                    intervals, page_size=self.pages[pid].size
                )
            else:
                ranges = ()
            if not ranges:
                raise ValidationError(
                    "diagnostic need must request at least one non-empty field, "
                    "record, or byte range"
                )
            return ResolvedNeed(
                need=need,
                page_id=pid,
                ranges=ranges,
                direct_bytes=sum(r.length for r in ranges),
            )
        except ValidationError as exc:
            reason = Reason(
                code="INVALID_SELECTOR",
                message=str(exc),
                page_id=need.page_id,
                details={"alias_id": need.alias_id, "record_id": need.record_id},
            )
            return ResolvedNeed(
                need=need,
                page_id=need.page_id,
                ranges=(),
                feasible=False,
                reasons=(reason,),
            )

    def _build_atoms(self) -> None:
        for pid, page in self.pages.items():
            boundary_ranges: list[ByteRange] = []
            boundary_ints: list[int] = [page.size]
            boundary_ranges.extend(self.allowed.get(pid, ()))
            boundary_ranges.extend(complement_ranges(self.allowed.get(pid, ()), page.size))
            boundary_ranges.extend(self.crop_ranges.get(pid, ()))
            boundary_ranges.extend(self.secret_ranges.get(pid, ()))
            for record in page.records:
                boundary_ranges.append(record.range)
                for f in record.fields:
                    boundary_ranges.append(f.range)
            for f in page.fields:
                boundary_ranges.append(f.range)
            for pinfo in self.pointer_infos:
                if pinfo.page_id == pid:
                    boundary_ranges.append(pinfo.range)
            for needs in self.target_needs.values():
                for rn in needs:
                    if rn.page_id == pid:
                        boundary_ranges.extend(rn.ranges)
            for rn in self.need_cache.values():
                if rn.page_id == pid:
                    boundary_ranges.extend(rn.ranges)

            points: set[int] = {0, page.size}
            for r in boundary_ranges:
                points.add(r.start)
                points.add(r.end)
            points.update(boundary_ints)
            ordered_points = sorted(p for p in points if 0 <= p <= page.size)
            page_atoms: list[Atom] = []
            for start, end in zip(ordered_points, ordered_points[1:]):
                if end <= start:
                    continue
                atom = Atom(pid, start, end)
                page_atoms.append(atom)
                self.atom_by_id[atom.atom_id] = atom
            self.atoms[pid] = page_atoms

    def _assess_all_needs(self) -> None:
        for tid, needs in self.target_needs.items():
            assessed = [self._assess_resolved_need(rn) for rn in needs]
            self.target_needs[tid] = assessed
            all_reasons: list[Reason] = []
            all_atom_ids: set[str] = set()
            feasible = True
            direct_bytes = 0
            for rn in assessed:
                feasible = feasible and rn.feasible
                all_reasons.extend(rn.reasons)
                all_atom_ids.update(rn.atom_ids)
                direct_bytes += rn.direct_bytes
            self.target_assessment[tid] = ResolvedNeed(
                need=FragmentNeed(need_id=tid),
                page_id=None,
                ranges=(),
                atom_ids=tuple(sorted(all_atom_ids)),
                feasible=feasible,
                reasons=tuple(self._with_target(r, tid) for r in all_reasons),
                direct_bytes=direct_bytes,
            )

        for key, rn in list(self.need_cache.items()):
            self.need_cache[key] = self._assess_resolved_need(rn)

    @staticmethod
    def _with_target(reason: Reason, target_id: str) -> Reason:
        return Reason(
            code=reason.code,
            message=reason.message,
            target_id=target_id,
            page_id=reason.page_id,
            domain=reason.domain,
            domains=reason.domains,
            range=reason.range,
            details=dict(reason.details),
        )

    def _assess_resolved_need(self, rn: ResolvedNeed) -> ResolvedNeed:
        if rn.page_id is None or not rn.ranges:
            return rn
        pid = rn.page_id
        reasons = list(rn.reasons)
        atom_ids: list[str] = []
        feasible = True

        common = self.allowed[pid]
        crops = self.crop_ranges[pid]
        for requested in rn.ranges:
            atom_ids.extend(self._atom_ids_for_range(pid, requested))
            security_gaps = uncovered_parts((requested,), common)
            crop_gaps = uncovered_parts((requested,), crops)
            for gap in security_gaps:
                feasible = False
                denying = [
                    d
                    for d in self.effective_domains[pid]
                    if not range_covers(self.domain_allowed[pid].get(d, ()), gap)
                ]
                if not self.effective_domains[pid]:
                    code = "NO_RELEASE_POLICY"
                    message = (
                        f"page {pid!r} has no coherent security domain policy; "
                        "release is denied by default"
                    )
                else:
                    code = "SECURITY_DENIED"
                    message = (
                        f"range {gap} is not permitted by all coherent domain policies"
                    )
                reasons.append(
                    Reason(
                        code=code,
                        message=message,
                        page_id=pid,
                        domains=tuple(denying),
                        range=gap,
                        details={
                            "coherent_domains": list(self.effective_domains[pid]),
                            "intersection_rule": "all coherent domains must permit",
                        },
                    )
                )
            for gap in crop_gaps:
                feasible = False
                reasons.append(
                    Reason(
                        code="CROPPED_BY_DUMPER",
                        message=f"range {gap} was removed by an optional dumper crop",
                        page_id=pid,
                        range=gap,
                    )
                )

        # De-duplicate atom IDs while preserving sorted order.
        atom_id_tuple = tuple(sorted(set(atom_ids)))
        return ResolvedNeed(
            need=rn.need,
            page_id=pid,
            ranges=rn.ranges,
            atom_ids=atom_id_tuple,
            feasible=feasible,
            reasons=tuple(reasons),
            direct_bytes=rn.direct_bytes,
        )

    def _atom_ids_for_range(self, page_id: str, r: ByteRange) -> list[str]:
        return [
            a.atom_id
            for a in self.atoms.get(page_id, ())
            if r.start <= a.start and a.end <= r.end
        ]

    # ------------------------------------------------------------- evaluation

    def _evaluate(self, seed_ids: Iterable[str]) -> Evaluation:
        seed_tuple = tuple(seed_ids)
        atom_ids: set[str] = set()
        hard_failure = False
        closure_reasons: list[Reason] = []

        for tid in seed_tuple:
            assessment = self.target_assessment[tid]
            if not assessment.feasible:
                hard_failure = True
                closure_reasons.extend(assessment.reasons)
            atom_ids.update(assessment.atom_ids)

        activated: set[str] = set()
        while True:
            changed = False
            for pinfo in self.pointer_infos:
                slot_atoms = self._atom_ids_for_range(pinfo.page_id, pinfo.range)
                if not slot_atoms or any(aid not in atom_ids for aid in slot_atoms):
                    continue
                if pinfo.key in activated:
                    continue
                activated.add(pinfo.key)

                if not self._pointer_is_clear(pinfo, atom_ids):
                    continue
                if not pinfo.spec.follow or not pinfo.spec.required:
                    # A retained but optional/reference-only pointer creates an
                    # explanatory need; the minimal plan does not buy extra
                    # downstream bytes unless another target selects them.
                    continue

                downstream_atoms, failure_reasons = self._pointer_downstream(pinfo)
                if failure_reasons:
                    closure_reasons.extend(failure_reasons)
                    seed_requires_hard = any(
                        self.targets[tid].require_pointer_closure for tid in seed_tuple
                    )
                    if seed_requires_hard:
                        hard_failure = True
                    continue
                new_ids = set(downstream_atoms) - atom_ids
                if new_ids:
                    atom_ids.update(new_ids)
                    changed = True
            if not changed:
                break

        retained_by_page = self._ranges_from_atoms(atom_ids)
        masked_by_page, pointer_status = self._compute_masks(
            retained_by_page, atom_ids
        )
        cost = sum(
            self.atom_by_id[aid].length
            for aid in atom_ids
            if aid in self.atom_by_id
        )
        budget = self.req.budget_bytes
        within_budget = budget is None or cost <= budget

        covered: list[str] = []
        complete: list[str] = []
        redacted: list[str] = []
        for tid in self.target_order:
            ass = self.target_assessment[tid]
            if not ass.feasible:
                continue
            if not set(ass.atom_ids).issubset(atom_ids):
                continue
            covered.append(tid)
            required_ranges: list[ByteRange] = []
            for rn in self.target_needs[tid]:
                required_ranges.extend(rn.ranges)
            is_redacted = any(
                intersect_unions(normalize_ranges(required_ranges), masked_by_page.get(pid, ()))
                for pid in {rn.page_id for rn in self.target_needs[tid] if rn.page_id}
            )
            if is_redacted:
                redacted.append(tid)
            else:
                complete.append(tid)

        # The optimization counts safe retained target scopes.  Mandatory
        # redaction is reported separately as a completeness loss.
        objective = self._objective_tuple(tuple(covered))
        signature = tuple(
            (pid, r.start, r.end)
            for pid in self.page_order
            for r in retained_by_page.get(pid, ())
        )
        return Evaluation(
            seed_ids=seed_tuple,
            atom_ids=set(atom_ids),
            cost=cost,
            within_budget=within_budget,
            hard_failure=hard_failure,
            retained_by_page=retained_by_page,
            masked_by_page=masked_by_page,
            supported_ids=tuple(covered),
            complete_ids=tuple(complete),
            redacted_ids=tuple(redacted),
            covered_ids=tuple(covered),
            objective=objective,
            signature=signature,
            closure_reasons=tuple(closure_reasons),
            pointer_status=pointer_status,
        )

    def _pointer_is_clear(self, pinfo: PointerInfo, atom_ids: set[str]) -> bool:
        slot_atoms = self._atom_ids_for_range(pinfo.page_id, pinfo.range)
        full_selected = bool(slot_atoms) and all(aid in atom_ids for aid in slot_atoms)
        if not full_selected or not pinfo.value_releasable:
            return False
        secret_overlap = intersect_unions(
            (pinfo.range,), self.secret_ranges.get(pinfo.page_id, ())
        )
        return not secret_overlap

    def _pointer_downstream(
        self, pinfo: PointerInfo
    ) -> tuple[set[str], tuple[Reason, ...]]:
        atoms: set[str] = set()
        reasons: list[Reason] = []
        if pinfo.spec.downstream is not None:
            rn = self.need_cache.get(id(pinfo.spec.downstream))
            if rn is None:
                rn = self._assess_resolved_need(
                    self._resolve_need_intervals(pinfo.spec.downstream)
                )
                self.need_cache[id(pinfo.spec.downstream)] = rn
            if not rn.feasible:
                reasons.extend(rn.reasons)
            else:
                atoms.update(rn.atom_ids)

        downstream_tid = pinfo.spec.downstream_target_id
        if downstream_tid is not None:
            if downstream_tid not in self.target_assessment:
                raise ValidationError(
                    f"pointer {pinfo.key!r} refers to unknown downstream target "
                    f"{downstream_tid!r}"
                )
            ass = self.target_assessment[downstream_tid]
            if not ass.feasible:
                reasons.extend(ass.reasons)
            else:
                atoms.update(ass.atom_ids)
        return atoms, tuple(reasons)

    def _ranges_from_atoms(
        self, atom_ids: set[str]
    ) -> dict[str, tuple[ByteRange, ...]]:
        ids_by_page: dict[str, list[Atom]] = {}
        for aid in atom_ids:
            atom = self.atom_by_id.get(aid)
            if atom is not None:
                ids_by_page.setdefault(atom.page_id, []).append(atom)
        out: dict[str, tuple[ByteRange, ...]] = {}
        for pid, atoms in ids_by_page.items():
            ranges = normalize_ranges([a.range for a in atoms])
            out[pid] = ranges
        return out

    def _compute_masks(
        self,
        retained_by_page: dict[str, tuple[ByteRange, ...]],
        atom_ids: set[str],
    ) -> tuple[dict[str, tuple[ByteRange, ...]], dict[str, str]]:
        masked: dict[str, list[ByteRange]] = {pid: [] for pid in self.page_order}
        status: dict[str, str] = {}

        for pid in self.page_order:
            retained = retained_by_page.get(pid, ())
            for secret in self.secret_ranges.get(pid, ()):
                masked[pid].extend(intersect_unions(retained, (secret,)))

        for pinfo in self.pointer_infos:
            retained = retained_by_page.get(pinfo.page_id, ())
            intersection = intersect_unions(retained, (pinfo.range,))
            clear = self._pointer_is_clear(pinfo, atom_ids)
            if not intersection:
                status[pinfo.key] = "omitted"
            elif clear:
                status[pinfo.key] = "clear_retained"
            else:
                masked[pinfo.page_id].extend(intersection)
                secret_overlap = bool(
                    intersect_unions(
                        (pinfo.range,),
                        self.secret_ranges.get(pinfo.page_id, ()),
                    )
                )
                if not pinfo.resolved and self.req.mask_unresolved_pointers:
                    status[pinfo.key] = "masked_unresolved"
                elif not pinfo.value_releasable:
                    status[pinfo.key] = "masked_restricted_address"
                elif secret_overlap:
                    status[pinfo.key] = "masked_address_pattern"
                else:
                    status[pinfo.key] = "masked_partial_pointer"

        normalized = {
            pid: normalize_ranges(ranges) for pid, ranges in masked.items()
        }
        return normalized, status

    def _objective_tuple(self, supported_ids: tuple[str, ...]) -> tuple[int, ...]:
        supported = set(supported_ids)
        if self.req.objective == "lexicographic":
            return tuple(
                sum(
                    1
                    for tid in supported
                    if self.targets[tid].priority == level
                )
                for level in self.priority_levels
            )
        if self.req.objective == "weighted":
            score = sum(self.targets[tid].priority for tid in supported)
            return (score, len(supported))
        return (len(supported), sum(self.targets[tid].priority for tid in supported))

    # ---------------------------------------------------------------- search

    def _search(self) -> tuple[Evaluation, dict[str, Any]]:
        feasible_ids = [
            tid
            for tid in self.target_order
            if self.target_assessment[tid].feasible
        ]

        def standalone_cost(tid: str) -> int:
            ev = self._evaluate([tid])
            return ev.cost if not ev.hard_failure else 10**18

        ordered_ids = sorted(
            feasible_ids,
            key=lambda tid: (
                -self.targets[tid].priority,
                standalone_cost(tid),
                tid,
            ),
        )

        empty = self._evaluate([])
        best: Evaluation | None = empty if empty.within_budget else None
        if best is None:
            # Budget cannot even hold the empty plan only if negative (validated);
            # keep an over-budget evaluation for reporting.
            best = empty

        # Deterministic greedy seeds provide a strong lower bound for pruning.
        greedy_seeds: list[str] = []
        for tid in ordered_ids:
            candidate = self._evaluate(greedy_seeds + [tid])
            if (
                not candidate.hard_failure
                and candidate.within_budget
                and self._is_better(candidate, best)
            ):
                greedy_seeds.append(tid)
                best = candidate

        m = len(ordered_ids)
        cache: dict[int, Evaluation] = {0: empty}
        stack: list[tuple[int, int]] = [(0, 0)]
        nodes = 0
        truncated = False
        exact = True

        while stack:
            pos, mask = stack.pop()
            nodes += 1
            if nodes > self.req.max_search_nodes:
                truncated = True
                exact = False
                break

            ev = cache.get(mask)
            if ev is None:
                seeds = [
                    ordered_ids[i]
                    for i in range(m)
                    if mask & (1 << i)
                ]
                ev = self._evaluate(seeds)
                cache[mask] = ev

            if ev.hard_failure or not ev.within_budget:
                continue

            if self._is_better(ev, best):
                best = ev

            if pos == m:
                continue

            # Optimistic upper bound: every structurally feasible target could
            # still become supported.  Cost cannot decrease below current cost.
            upper = self._objective_tuple(tuple(feasible_ids))
            explore_for_cost = True
            if upper < best.objective:
                continue
            if upper == best.objective and ev.cost >= best.cost:
                explore_for_cost = False
            if not explore_for_cost:
                continue

            bit = 1 << pos
            # Push exclusion first so inclusion is popped first.
            stack.append((pos + 1, mask))
            include_mask = mask | bit
            seeds = [
                ordered_ids[i]
                for i in range(pos + 1)
                if include_mask & (1 << i)
            ]
            include_ev = self._evaluate(seeds)
            cache[include_mask] = include_ev
            if not include_ev.hard_failure and include_ev.within_budget:
                stack.append((pos + 1, include_mask))

        assert best is not None
        info = {
            "search_nodes": nodes,
            "candidate_targets": m,
            "exact": exact and not truncated,
            "search_truncated": truncated,
            "max_search_nodes": self.req.max_search_nodes,
            "ordered_targets": ordered_ids,
        }
        self.search_info = info
        return best, info

    def _is_better(self, candidate: Evaluation, best: Evaluation | None) -> bool:
        if best is None:
            return True
        if candidate.hard_failure:
            return False
        if candidate.objective != best.objective:
            return candidate.objective > best.objective
        if candidate.cost != best.cost:
            return candidate.cost < best.cost
        return candidate.signature < best.signature

    # -------------------------------------------------------------- reporting

    def _build_plan(
        self, best: Evaluation, search_info: dict[str, Any]
    ) -> DumpPlan:
        page_plans: dict[str, PagePlan] = {}
        fragments: list[Fragment] = []
        plan_reasons: list[Reason] = []

        for pid in self.page_order:
            retained = best.retained_by_page.get(pid, ())
            masked = best.masked_by_page.get(pid, ())
            denied = complement_ranges(self.allowed[pid], self.pages[pid].size)
            page_plan = PagePlan(
                page_id=pid,
                output_handle=self.handle[pid],
                role=self.pages[pid].role,
                size=self.pages[pid].size,
                coherent_domains=tuple(
                    d for d in self.effective_domains[pid] if d != DEFAULT_POLICY_DOMAIN
                ),
                allowed_ranges=self.allowed[pid],
                denied_ranges=denied,
                crop_ranges=self.crop_ranges[pid],
                address_allowed=self.address_allowed[pid],
                retained_ranges=retained,
                masked_ranges=masked,
                domain_allowed_ranges=self.domain_allowed[pid],
            )
            page_plans[pid] = page_plan

            for r in retained:
                fragment_mask_reasons: list[Reason] = []
                for mr in masked:
                    if r.intersects(mr):
                        pstatus = self._mask_reason_for_range(pid, mr, best)
                        if pstatus is not None:
                            fragment_mask_reasons.append(pstatus)
                frag = Fragment(
                    page_id=pid,
                    output_handle=self.handle[pid],
                    start=r.start,
                    end=r.end,
                    masked_ranges=tuple(
                        mr for mr in masked if r.intersects(mr)
                    ),
                    reasons=tuple(fragment_mask_reasons),
                )
                fragments.append(frag)
                plan_reasons.extend(fragment_mask_reasons)

        pointer_decisions = self._build_pointer_decisions(best)
        target_decisions = self._build_target_decisions(best)
        for td in target_decisions:
            plan_reasons.extend(td.reasons)
        for pd in pointer_decisions:
            if pd.reason is not None:
                plan_reasons.append(pd.reason)

        retained_bytes = sum(f.length for f in fragments)
        budget_ok = (
            self.req.budget_bytes is None
            or retained_bytes <= self.req.budget_bytes
        )
        return DumpPlan(
            budget_bytes=self.req.budget_bytes,
            objective=self.req.objective,
            retained_bytes=retained_bytes,
            fragment_count=len(fragments),
            feasible=budget_ok and not best.hard_failure,
            pages=page_plans,
            fragments=tuple(fragments),
            targets=tuple(target_decisions),
            pointer_decisions=tuple(pointer_decisions),
            reasons=tuple(plan_reasons),
            optimization=search_info,
        )

    def _mask_reason_for_range(
        self, page_id: str, mr: ByteRange, best: Evaluation
    ) -> Reason | None:
        for pinfo in self.pointer_infos:
            if pinfo.page_id == page_id and pinfo.range.intersects(mr):
                status = best.pointer_status.get(pinfo.key, "masked")
                if status == "masked_restricted_address":
                    return Reason(
                        code="POINTER_MASKED_RESTRICTED_ADDRESS",
                        message=(
                            "pointer bytes are zeroized because its target page/address "
                            "is not releasable under all coherent policies"
                        ),
                        page_id=page_id,
                        range=mr,
                        details={
                            "pointer": pinfo.key,
                            "target_page": pinfo.target_page,
                            "downstream_read_created": False,
                        },
                    )
                if status == "masked_unresolved":
                    return Reason(
                        code="POINTER_MASKED_UNRESOLVED",
                        message="unresolved pointer bytes are zeroized by safe default",
                        page_id=page_id,
                        range=mr,
                        details={"pointer": pinfo.key},
                    )
                if status == "masked_partial_pointer":
                    return Reason(
                        code="PARTIAL_POINTER_MASKED",
                        message=(
                            "only part of a pointer is retained; the retained address "
                            "bytes are masked and no downstream read is created"
                        ),
                        page_id=page_id,
                        range=mr,
                        details={"pointer": pinfo.key},
                    )
        if any(
            sr.intersects(mr) for sr in self.secret_ranges.get(page_id, ())
        ):
            return Reason(
                code="RESTRICTED_ADDRESS_PATTERN_MASKED",
                message="an encoded restricted address was found and zeroized",
                page_id=page_id,
                range=mr,
            )
        return Reason(
            code="ADDRESS_RANGE_MASKED",
            message="address-bearing bytes were masked",
            page_id=page_id,
            range=mr,
        )

    def _build_pointer_decisions(self, best: Evaluation) -> list[PointerDecision]:
        out: list[PointerDecision] = []
        for pinfo in self.pointer_infos:
            status = best.pointer_status.get(pinfo.key, "omitted")
            retained = status != "omitted"
            clear = status == "clear_retained"
            reason: Reason | None = None
            if status == "clear_retained":
                downstream_count = 0
                atoms, _ = self._pointer_downstream(pinfo)
                downstream_count = len(atoms)
                reason = Reason(
                    code="POINTER_RETAINED_DOWNSTREAM_REQUIRED",
                    message=(
                        "full releasable pointer retained; its downstream selector "
                        "creates a read requirement"
                    ),
                    page_id=pinfo.page_id,
                    range=pinfo.range,
                    details={
                        "pointer": pinfo.key,
                        "target_page": pinfo.target_page,
                        "downstream_atom_count": downstream_count,
                    },
                )
            elif status.startswith("masked"):
                reason = self._mask_reason_for_range(pinfo.page_id, pinfo.range, best)
            else:
                reason = Reason(
                    code="POINTER_NOT_RETAINED",
                    message=(
                        "pointer slot is outside the minimal selected ranges; "
                        "therefore it creates no downstream read requirement"
                    ),
                    page_id=pinfo.page_id,
                    range=pinfo.range,
                    details={"pointer": pinfo.key},
                )
            out.append(
                PointerDecision(
                    pointer_key=pinfo.key,
                    page_id=pinfo.page_id,
                    range=pinfo.range,
                    status=status,
                    retained=retained,
                    clear_pointer_retained=clear,
                    target_page=pinfo.target_page,
                    downstream_atom_count=(
                        len(self._pointer_downstream(pinfo)[0]) if clear else 0
                    ),
                    reason=reason,
                )
            )
        return out

    def _build_target_decisions(self, best: Evaluation) -> list[TargetDecision]:
        out: list[TargetDecision] = []
        best_seed_set = set(best.seed_ids)
        for tid in self.target_order:
            target = self.targets[tid]
            ass = self.target_assessment[tid]
            reasons: list[Reason] = []
            status: str
            covered = tid in best.covered_ids
            supported = covered
            complete = tid in best.complete_ids
            redacted = tid in best.redacted_ids
            selected_root = tid in best_seed_set

            standalone = self._evaluate([tid]) if ass.feasible else None
            required_bytes = (
                standalone.cost
                if standalone is not None and not standalone.hard_failure
                else ass.direct_bytes
            )

            if covered and redacted:
                status = "retained_redacted"
                reasons.append(
                    Reason(
                        code="TARGET_RETAINED_WITH_REDACTION",
                        message=(
                            "required scope is retained as a safe fragment, but "
                            "address-bearing bytes are masked and cannot be used as "
                            "an intact indirect pointer"
                        ),
                        target_id=tid,
                    )
                )
                reasons.extend(self._redaction_reasons(tid, best))
            elif supported:
                status = "retained"
                complete = True
                reasons.append(
                    Reason(
                        code="TARGET_SUPPORTED",
                        message="all required fragments are retained without redaction",
                        target_id=tid,
                        details={"priority": target.priority},
                    )
                )
            elif not ass.feasible:
                status = self._dominant_failure_status(ass.reasons)
                reasons.extend(ass.reasons)
            else:
                # Feasible alone, but not in the optimal supported set.
                trial = self._evaluate(list(best_seed_set) + [tid])
                if trial.hard_failure:
                    status = "downstream_denied"
                    reasons.extend(trial.closure_reasons)
                    reasons.append(
                        Reason(
                            code="TARGET_DISCARDED_DOWNSTREAM",
                            message=(
                                "retaining this target's intact pointer would create "
                                "a downstream read that security policy forbids"
                            ),
                            target_id=tid,
                        )
                    )
                elif not trial.within_budget:
                    available = (
                        self.req.budget_bytes - best.cost
                        if self.req.budget_bytes is not None
                        else None
                    )
                    marginal = max(0, trial.cost - best.cost)
                    status = "budget_denied"
                    reasons.append(
                        Reason(
                            code="BUDGET_EXCEEDED",
                            message=(
                                "supporting this target requires bytes beyond the "
                                "dump budget"
                            ),
                            target_id=tid,
                            details={
                                "standalone_required_bytes": required_bytes,
                                "optimal_plan_bytes": best.cost,
                                "combined_plan_bytes": trial.cost,
                                "marginal_bytes": marginal,
                                "remaining_budget_bytes": available,
                                "deficit_bytes": (
                                    None
                                    if self.req.budget_bytes is None
                                    else max(
                                        0,
                                        trial.cost - self.req.budget_bytes,
                                    )
                                ),
                            },
                        )
                    )
                else:
                    truncated = bool(self.search_info.get("search_truncated"))
                    status = "search_limit" if truncated else "not_selected"
                    reasons.append(
                        Reason(
                            code=(
                                "SEARCH_NODE_LIMIT_REACHED"
                                if truncated
                                else "TARGET_NOT_SELECTED"
                            ),
                            message=(
                                "target could improve the plan but the exact search "
                                "hit its configured node limit"
                                if truncated
                                else (
                                    "target was not selected by the deterministic "
                                    "search; this indicates an unexpected optimizer state"
                                )
                            ),
                            target_id=tid,
                            details={
                                "search_truncated": truncated,
                                "max_search_nodes": self.req.max_search_nodes,
                            },
                        )
                    )

            out.append(
                TargetDecision(
                    target_id=tid,
                    priority=target.priority,
                    status=status,
                    supported=supported,
                    complete=complete,
                    redacted=redacted,
                    selected_as_root=selected_root,
                    supported_as_dependency=supported and not selected_root,
                    required_bytes=required_bytes,
                    marginal_bytes=(
                        max(0, self._evaluate(list(best_seed_set) + [tid]).cost - best.cost)
                        if ass.feasible and not supported
                        else None
                    ),
                    reasons=tuple(reasons),
                )
            )
        return out

    def _redaction_reasons(self, tid: str, best: Evaluation) -> list[Reason]:
        out: list[Reason] = []
        for rn in self.target_needs[tid]:
            if rn.page_id is None:
                continue
            masked = best.masked_by_page.get(rn.page_id, ())
            for r in rn.ranges:
                for overlap in intersect_unions((r,), masked):
                    reason = self._mask_reason_for_range(rn.page_id, overlap, best)
                    if reason is not None:
                        out.append(self._with_target(reason, tid))
        return out

    @staticmethod
    def _dominant_failure_status(reasons: tuple[Reason, ...]) -> str:
        codes = {r.code for r in reasons}
        if "SECURITY_DENIED" in codes or "NO_RELEASE_POLICY" in codes:
            return "security_denied"
        if "CROPPED_BY_DUMPER" in codes:
            return "cropped"
        return "invalid_selector"
