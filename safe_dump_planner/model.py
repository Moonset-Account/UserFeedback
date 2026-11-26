"""Input data model for the safe dump planner.

The model is intentionally plain and JSON-friendly.  In addition to constructing
dataclasses directly, callers can pass a dictionary to :func:`plan_safe_dump`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Sequence

from .errors import ValidationError
from .geometry import ByteRange, coerce_ranges


PAGE_ROLES = ("generic", "stack", "allocation")
OBJECT_KINDS = ("generic", "stack_frame", "allocation", "allocation_record")


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _maybe_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass
class FieldSpec:
    """A named field at a byte offset."""

    name: str
    offset: int
    size: int = 8
    kind: str = "data"

    def __post_init__(self) -> None:
        self.offset = int(self.offset)
        self.size = int(self.size)
        if self.offset < 0 or self.size <= 0:
            raise ValidationError(
                f"field {self.name!r} must have non-negative offset and positive size"
            )
        if self.kind not in ("data", "pointer"):
            raise ValidationError(f"unsupported field kind {self.kind!r}")

    @property
    def range(self) -> ByteRange:
        return ByteRange(self.offset, self.offset + self.size)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "FieldSpec") -> "FieldSpec":
        if isinstance(value, FieldSpec):
            return value
        return cls(
            name=value["name"],
            offset=value.get("offset", value.get("start")),
            size=value.get("size", value.get("length", 8)),
            kind=value.get("kind", "data"),
        )


@dataclass
class RecordSpec:
    """A stack frame or allocation record contained in a page."""

    record_id: str
    offset: int
    size: int
    kind: str = "allocation"
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.offset = int(self.offset)
        self.size = int(self.size)
        if self.offset < 0 or self.size <= 0:
            raise ValidationError(
                f"record {self.record_id!r} must have non-negative offset and positive size"
            )
        if self.kind not in OBJECT_KINDS:
            raise ValidationError(f"unsupported record kind {self.kind!r}")
        self.fields = tuple(FieldSpec.from_dict(f) for f in self.fields)

    @property
    def range(self) -> ByteRange:
        return ByteRange(self.offset, self.offset + self.size)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "RecordSpec") -> "RecordSpec":
        if isinstance(value, RecordSpec):
            return value
        return cls(
            record_id=value.get("record_id", value.get("id")),
            offset=value["offset"],
            size=value["size"],
            kind=value.get("kind", "allocation"),
            fields=tuple(value.get("fields", ())),
        )


@dataclass
class PageSpec:
    """A physical page and its optional synthetic contents."""

    page_id: str
    size: int | None = None
    data: bytes | bytearray | None = None
    role: str = "generic"
    owner_domain: str | None = None
    coherent_domains: tuple[str, ...] = field(default_factory=tuple)
    physical_address: int | None = None
    records: tuple[RecordSpec, ...] = field(default_factory=tuple)
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.role not in PAGE_ROLES:
            raise ValidationError(
                f"page {self.page_id!r} role must be one of {PAGE_ROLES}, got {self.role!r}"
            )
        if self.data is not None and not isinstance(self.data, (bytes, bytearray)):
            raise ValidationError("page data must be bytes or bytearray")
        if isinstance(self.data, bytearray):
            self.data = bytes(self.data)
        if self.size is None:
            self.size = len(self.data) if self.data is not None else 4096
        self.size = int(self.size)
        if self.size <= 0:
            raise ValidationError(f"page {self.page_id!r} size must be positive")
        if self.data is not None and len(self.data) > self.size:
            raise ValidationError(
                f"page {self.page_id!r} data length {len(self.data)} exceeds size {self.size}"
            )
        self.coherent_domains = _tuple_of_str(self.coherent_domains)
        self.records = tuple(RecordSpec.from_dict(r) for r in self.records)
        self.fields = tuple(FieldSpec.from_dict(f) for f in self.fields)
        if self.physical_address is not None:
            self.physical_address = int(self.physical_address)

    @property
    def padded_data(self) -> bytes:
        """Synthetic page bytes, zero-padded to the declared page size."""

        if self.data is None:
            return bytes(self.size)
        if len(self.data) < self.size:
            return bytes(self.data) + bytes(self.size - len(self.data))
        return bytes(self.data)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "PageSpec") -> "PageSpec":
        if isinstance(value, PageSpec):
            return value
        data = value.get("data")
        if "data_hex" in value:
            data = bytes.fromhex(value["data_hex"])
        elif isinstance(data, str):
            try:
                data = bytes.fromhex(data)
            except ValueError:
                data = data.encode("utf-8")
        elif isinstance(data, list):
            data = bytes(data)
        return cls(
            page_id=value.get("page_id", value.get("id")),
            size=value.get("size"),
            data=data,
            role=value.get("role", value.get("usage", "generic")),
            owner_domain=value.get("owner_domain"),
            coherent_domains=value.get("coherent_domains", ()),
            physical_address=value.get("physical_address"),
            records=tuple(value.get("records", ())),
            fields=tuple(value.get("fields", ())),
        )


@dataclass(frozen=True)
class VirtualAlias:
    """A virtual address/alias mapping to a physical page in one domain."""

    alias_id: str
    page_id: str
    virtual_address: int | None = None
    domain: str | None = None

    def __post_init__(self) -> None:
        if self.virtual_address is not None:
            object.__setattr__(self, "virtual_address", int(self.virtual_address))

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "VirtualAlias") -> "VirtualAlias":
        if isinstance(value, VirtualAlias):
            return value
        return cls(
            alias_id=value.get("alias_id", value.get("id")),
            page_id=value["page_id"],
            virtual_address=value.get("virtual_address", value.get("address")),
            domain=value.get("domain"),
        )


@dataclass
class FragmentNeed:
    """A request for fields, records, or byte ranges on one page/alias."""

    need_id: str | None = None
    page_id: str | None = None
    alias_id: str | None = None
    ranges: tuple[ByteRange, ...] = ()
    field_names: tuple[str, ...] = ()
    record_id: str | None = None

    def __post_init__(self) -> None:
        self.ranges = coerce_ranges(self.ranges)
        self.field_names = _tuple_of_str(self.field_names)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "FragmentNeed") -> "FragmentNeed":
        if isinstance(value, FragmentNeed):
            return value
        return cls(
            need_id=value.get("need_id", value.get("id")),
            page_id=value.get("page_id"),
            alias_id=value.get("alias_id"),
            ranges=value.get("ranges", ()),
            field_names=value.get("field_names", value.get("fields", ())),
            record_id=value.get("record_id"),
        )


@dataclass
class Crop:
    """An optional dumper allowlist of fields and/or byte ranges."""

    crop_id: str | None = None
    page_id: str | None = None
    alias_id: str | None = None
    ranges: tuple[ByteRange, ...] = ()
    field_names: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    whole_page: bool = False

    def __post_init__(self) -> None:
        self.ranges = coerce_ranges(self.ranges)
        self.field_names = _tuple_of_str(self.field_names)
        self.record_ids = _tuple_of_str(self.record_ids)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "Crop") -> "Crop":
        if isinstance(value, Crop):
            return value
        return cls(
            crop_id=value.get("crop_id", value.get("id")),
            page_id=value.get("page_id"),
            alias_id=value.get("alias_id"),
            ranges=value.get("ranges", ()),
            field_names=value.get("field_names", value.get("fields", ())),
            record_ids=value.get("record_ids", ()),
            whole_page=bool(value.get("whole_page", False)),
        )


@dataclass
class PointerRef:
    """An explicit pointer slot and the read need created if it stays intact."""

    offset: int
    size: int = 8
    pointer_id: str | None = None
    label: str | None = None
    page_id: str | None = None
    alias_id: str | None = None
    target_page: str | None = None
    target_alias_id: str | None = None
    target_virtual_address: int | None = None
    domain: str | None = None
    value: int | None = None
    follow: bool = True
    required: bool = True
    downstream: FragmentNeed | None = None
    downstream_target_id: str | None = None

    def __post_init__(self) -> None:
        self.offset = int(self.offset)
        self.size = int(self.size)
        if self.offset < 0 or self.size <= 0:
            raise ValidationError("pointer offset must be non-negative and size positive")
        if self.value is not None:
            self.value = int(self.value)
        if self.target_virtual_address is not None:
            self.target_virtual_address = int(self.target_virtual_address)
        if isinstance(self.downstream, dict):
            self.downstream = FragmentNeed.from_dict(self.downstream)

    @property
    def range(self) -> ByteRange:
        return ByteRange(self.offset, self.offset + self.size)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "PointerRef") -> "PointerRef":
        if isinstance(value, PointerRef):
            return value
        downstream = value.get("downstream")
        return cls(
            pointer_id=value.get("pointer_id", value.get("id")),
            label=value.get("label", value.get("name")),
            page_id=value.get("page_id"),
            alias_id=value.get("alias_id"),
            offset=value["offset"],
            size=value.get("size", 8),
            target_page=value.get("target_page"),
            target_alias_id=value.get("target_alias_id"),
            target_virtual_address=value.get(
                "target_virtual_address", value.get("target_address")
            ),
            domain=value.get("domain"),
            value=value.get("value"),
            follow=bool(value.get("follow", True)),
            required=bool(value.get("required", True)),
            downstream=(
                downstream
                if downstream is None or isinstance(downstream, FragmentNeed)
                else FragmentNeed.from_dict(downstream)
            ),
            downstream_target_id=value.get("downstream_target_id"),
        )


@dataclass(frozen=True)
class PolicyRule:
    """A page rule containing whole-page, range, and named-field grants."""

    page_id: str = "*"
    whole_page: bool = False
    ranges: tuple[ByteRange, ...] = ()
    field_names: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ranges", coerce_ranges(self.ranges))
        object.__setattr__(self, "field_names", _tuple_of_str(self.field_names))

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "PolicyRule") -> "PolicyRule":
        if isinstance(value, PolicyRule):
            return value
        return cls(
            page_id=value.get("page_id", "*"),
            whole_page=bool(value.get("whole_page", False)),
            ranges=value.get("ranges", ()),
            field_names=value.get("field_names", value.get("fields", ())),
        )


@dataclass
class DomainPolicy:
    """Release permissions for one security domain.

    Content is allowed only by ``allow_all_content`` or an entry in ``allow``;
    entries in ``deny`` subtract bytes and therefore override allows.  Addresses
    are separate from content because an allowed data fragment can still contain
    an address that a domain is not permitted to disclose.
    """

    domain: str
    allow_all_content: bool = False
    allow_all_addresses: bool = False
    allow_address_pages: tuple[str, ...] = ()
    allow: tuple[PolicyRule, ...] = ()
    deny: tuple[PolicyRule, ...] = ()
    deny_address_pages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.allow_address_pages = _tuple_of_str(self.allow_address_pages)
        self.allow = tuple(PolicyRule.from_dict(r) for r in self.allow)
        self.deny = tuple(PolicyRule.from_dict(r) for r in self.deny)
        self.deny_address_pages = _tuple_of_str(self.deny_address_pages)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "DomainPolicy") -> "DomainPolicy":
        if isinstance(value, DomainPolicy):
            return value
        return cls(
            domain=value["domain"],
            allow_all_content=bool(value.get("allow_all_content", value.get("allow_all", False))),
            allow_all_addresses=bool(
                value.get("allow_all_addresses", value.get("release_addresses", False))
            ),
            allow_address_pages=value.get("allow_address_pages", ()),
            allow=tuple(value.get("allow", value.get("grants", ()))),
            deny=tuple(value.get("deny", value.get("denials", ()))),
            deny_address_pages=value.get("deny_address_pages", ()),
        )

    @classmethod
    def full(
        cls,
        domain: str,
        *,
        addresses: bool = True,
    ) -> "DomainPolicy":
        return cls(
            domain=domain,
            allow_all_content=True,
            allow_all_addresses=addresses,
        )


@dataclass
class DiagnosticTarget:
    """A prioritized diagnostic fact that needs one or more fragments."""

    target_id: str
    priority: int = 0
    page_id: str | None = None
    alias_id: str | None = None
    ranges: tuple[ByteRange, ...] = ()
    field_names: tuple[str, ...] = ()
    record_id: str | None = None
    needs: tuple[FragmentNeed, ...] = ()
    require_pointer_closure: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        self.priority = int(self.priority)
        self.ranges = coerce_ranges(self.ranges)
        self.field_names = _tuple_of_str(self.field_names)
        self.needs = tuple(FragmentNeed.from_dict(n) for n in self.needs)

    def direct_need(self) -> FragmentNeed | None:
        if self.page_id is None and self.alias_id is None:
            if self.ranges or self.field_names or self.record_id:
                raise ValidationError(
                    f"target {self.target_id!r} has selectors but no page_id/alias_id"
                )
            return None
        return FragmentNeed(
            need_id=f"{self.target_id}:root",
            page_id=self.page_id,
            alias_id=self.alias_id,
            ranges=self.ranges,
            field_names=self.field_names,
            record_id=self.record_id,
        )

    def all_needs(self) -> tuple[FragmentNeed, ...]:
        root = self.direct_need()
        return ((root,) if root is not None else ()) + self.needs

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "DiagnosticTarget") -> "DiagnosticTarget":
        if isinstance(value, DiagnosticTarget):
            return value
        return cls(
            target_id=value.get("target_id", value.get("id")),
            priority=value.get("priority", 0),
            page_id=value.get("page_id"),
            alias_id=value.get("alias_id"),
            ranges=value.get("ranges", ()),
            field_names=value.get("field_names", value.get("fields", ())),
            record_id=value.get("record_id"),
            needs=tuple(value.get("needs", ())),
            require_pointer_closure=bool(value.get("require_pointer_closure", True)),
            description=value.get("description", ""),
        )


@dataclass
class DumpRequest:
    """All inputs needed to produce a safe retention plan."""

    pages: tuple[PageSpec, ...]
    policies: tuple[DomainPolicy, ...] = ()
    aliases: tuple[VirtualAlias, ...] = ()
    targets: tuple[DiagnosticTarget, ...] = ()
    crops: tuple[Crop, ...] = ()
    pointers: tuple[PointerRef, ...] = ()
    budget_bytes: int | None = None
    objective: str = "lexicographic"
    redaction_byte: int = 0
    mask_unresolved_pointers: bool = True
    scan_text_addresses: bool = True
    strict: bool = True
    default_policy: DomainPolicy | None = None
    max_search_nodes: int = 500_000

    def __post_init__(self) -> None:
        self.pages = tuple(PageSpec.from_dict(p) for p in self.pages)
        self.policies = tuple(DomainPolicy.from_dict(p) for p in self.policies)
        self.aliases = tuple(VirtualAlias.from_dict(a) for a in self.aliases)
        self.targets = tuple(DiagnosticTarget.from_dict(t) for t in self.targets)
        self.crops = tuple(Crop.from_dict(c) for c in self.crops)
        self.pointers = tuple(PointerRef.from_dict(p) for p in self.pointers)
        if self.default_policy is not None:
            self.default_policy = DomainPolicy.from_dict(self.default_policy)
        if self.budget_bytes is not None:
            self.budget_bytes = int(self.budget_bytes)
            if self.budget_bytes < 0:
                raise ValidationError("budget_bytes must be non-negative")
        if not 0 <= int(self.redaction_byte) <= 255:
            raise ValidationError("redaction_byte must be in [0,255]")
        if self.objective not in ("lexicographic", "weighted", "count"):
            raise ValidationError(f"unknown optimization objective {self.objective!r}")
        self.max_search_nodes = int(self.max_search_nodes)
        if self.max_search_nodes < 1:
            raise ValidationError("max_search_nodes must be positive")

    @classmethod
    def from_dict(cls, value: dict[str, Any] | "DumpRequest") -> "DumpRequest":
        if isinstance(value, DumpRequest):
            return value
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in value.items() if k in known}
        return cls(**kwargs)
