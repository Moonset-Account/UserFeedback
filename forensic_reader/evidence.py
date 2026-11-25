"""Known-byte and page-digest evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from .checksums import digest_hex, normalize_digest
from .errors import EvidenceFormatError


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceFormatError(f"{name} must be an integer")
    if value < 0:
        raise EvidenceFormatError(f"{name} must be non-negative")
    return value


def _byte_value(value: object) -> int:
    if isinstance(value, bool):
        raise EvidenceFormatError("byte value must be an integer 0..255")
    if isinstance(value, int):
        number = value
    elif isinstance(value, (bytes, bytearray)) and len(value) == 1:
        number = value[0]
    else:
        raise EvidenceFormatError("byte value must be an integer 0..255 or one-byte bytes")
    if not 0 <= number <= 255:
        raise EvidenceFormatError("byte value must be in 0..255")
    return number


@dataclass(frozen=True)
class EvidenceEvaluation:
    ok: bool
    reason: str | None = None
    message: str | None = None
    expected: Any = None
    actual: Any = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class KnownByte:
    """A sourced known byte at a logical image address."""

    address: int
    value: int
    source: str | None = None
    note: str | None = None
    kind: ClassVar[str] = "known_byte"

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", _nonnegative_int(self.address, "address"))
        object.__setattr__(self, "value", _byte_value(self.value))
        if self.source is not None and not isinstance(self.source, str):
            object.__setattr__(self, "source", str(self.source))

    def validate(self, image_length: int) -> None:
        if self.address >= image_length:
            raise EvidenceFormatError(
                f"known byte address {self.address} is outside logical image length {image_length}"
            )

    def verify(self, image: bytes) -> EvidenceEvaluation:
        self.validate(len(image))
        actual = image[self.address]
        if actual == self.value:
            return EvidenceEvaluation(ok=True)
        return EvidenceEvaluation(
            ok=False,
            reason="known_byte_mismatch",
            message=(
                f"known byte at 0x{self.address:x} expected 0x{self.value:02x}, "
                f"found 0x{actual:02x}"
            ),
            expected=self.value,
            actual=actual,
            details={"address": self.address, "source": self.source},
        )

    def summary(self) -> str:
        source = f" from {self.source}" if self.source else ""
        return f"known byte 0x{self.address:x}=0x{self.value:02x}{source}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "address": self.address,
            "address_hex": f"0x{self.address:x}",
            "value": self.value,
            "value_hex": f"0x{self.value:02x}",
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class PageChecksum:
    """An expected digest for a logical page or explicit logical interval."""

    expected_digest: str
    algorithm: str = "sha256"
    source: str | None = None
    note: str | None = None
    page: int | None = None
    page_size: int | None = None
    start: int | None = None
    end: int | None = None
    kind: ClassVar[str] = "page_checksum"

    def __post_init__(self) -> None:
        algo, digest = normalize_digest(self.expected_digest, algorithm=self.algorithm)
        object.__setattr__(self, "algorithm", algo)
        object.__setattr__(self, "expected_digest", digest)
        for name in ("page", "page_size", "start", "end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _nonnegative_int(value, name))
        if self.page_size is not None and self.page_size <= 0:
            raise EvidenceFormatError("page_size must be positive")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise EvidenceFormatError("checksum interval end must be greater than start")

    @classmethod
    def for_page(
        cls,
        page: int,
        page_size: int,
        digest: str | bytes,
        *,
        algorithm: str | None = None,
        source: str | None = None,
        note: str | None = None,
    ) -> "PageChecksum":
        return cls(
            page=page,
            page_size=page_size,
            expected_digest=digest,
            algorithm=algorithm or "sha256",
            source=source,
            note=note,
        )

    @classmethod
    def for_range(
        cls,
        start: int,
        end: int,
        digest: str | bytes,
        *,
        algorithm: str | None = None,
        source: str | None = None,
        note: str | None = None,
    ) -> "PageChecksum":
        return cls(
            start=start,
            end=end,
            expected_digest=digest,
            algorithm=algorithm or "sha256",
            source=source,
            note=note,
        )

    def resolve_range(self, image_length: int) -> tuple[int, int]:
        if self.start is not None or self.end is not None:
            if self.start is None or self.end is None:
                raise EvidenceFormatError("both start and end are required for an explicit interval")
            start, end = self.start, self.end
        else:
            if self.page is None or self.page_size is None:
                raise EvidenceFormatError(
                    "page checksum requires page and page_size, or start and end"
                )
            start = self.page * self.page_size
            end = start + self.page_size
        if end > image_length:
            raise EvidenceFormatError(
                f"checksum interval [{start}, {end}) exceeds logical image length {image_length}"
            )
        return start, end

    def validate(self, image_length: int) -> None:
        self.resolve_range(image_length)

    def verify(self, image: bytes) -> EvidenceEvaluation:
        start, end = self.resolve_range(len(image))
        actual = digest_hex(self.algorithm, image[start:end])
        if actual == self.expected_digest:
            return EvidenceEvaluation(ok=True, details={"start": start, "end": end})
        return EvidenceEvaluation(
            ok=False,
            reason="checksum_mismatch",
            message=(
                f"{self.algorithm} digest for [{start}, {end}) expected "
                f"{self.expected_digest}, found {actual}"
            ),
            expected=self.expected_digest,
            actual=actual,
            details={
                "algorithm": self.algorithm,
                "start": start,
                "end": end,
                "page": self.page,
                "source": self.source,
            },
        )

    def summary(self) -> str:
        if self.start is not None and self.end is not None:
            target = f"interval [{self.start},{self.end})"
        else:
            target = f"page {self.page} of length {self.page_size}"
        source = f" from {self.source}" if self.source else ""
        return f"{self.algorithm} checksum for {target}{source}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "page": self.page,
            "page_size": self.page_size,
            "start": self.start,
            "end": self.end,
            "algorithm": self.algorithm,
            "expected_digest": self.expected_digest,
            "source": self.source,
            "note": self.note,
        }


Evidence = KnownByte | PageChecksum


def coerce_evidence(item: object, **defaults: Any) -> Evidence:
    """Coerce a dict/tuple/evidence object into a validated evidence object."""

    if isinstance(item, (KnownByte, PageChecksum)):
        return item
    if isinstance(item, tuple) and len(item) in (2, 3) and all(isinstance(x, int) for x in item[:2]):
        # (address, value) or (address, value, source)
        source = item[2] if len(item) == 3 else defaults.get("source")
        return KnownByte(item[0], item[1], source=source)  # type: ignore[arg-type]
    if not isinstance(item, dict):
        raise EvidenceFormatError(f"unsupported evidence item: {item!r}")

    data = {**defaults, **item}
    kind = str(data.get("type") or data.get("kind") or "").strip().lower()

    def first_present(*names: str) -> Any:
        for name in names:
            if name in data and data[name] is not None:
                return data[name]
        return None

    if kind in {"byte", "known", "known_byte", "known-byte"}:
        return KnownByte(
            address=first_present("address", "offset", "addr", "position", "location"),
            value=first_present("value", "byte", "expected_byte", "expected"),
            source=data.get("source"),
            note=data.get("note"),
        )
    if kind in {"page", "page_checksum", "checksum", "digest", "page-digest"}:
        return _page_from_dict(data)

    address = first_present("address", "offset", "addr", "position", "location")
    value = first_present("value", "byte", "expected_byte")
    if address is not None and value is not None:
        return KnownByte(
            address=address,
            value=value,
            source=data.get("source"),
            note=data.get("note"),
        )
    if any(key in data for key in ("digest", "checksum", "expected_digest", "hash")):
        return _page_from_dict(data)
    raise EvidenceFormatError(f"could not determine evidence type from: {item!r}")


def _page_from_dict(data: dict[str, Any]) -> PageChecksum:
    def first(*names: str) -> Any:
        for name in names:
            value = data.get(name)
            if value is not None:
                return value
        return None

    digest = first("digest", "checksum", "expected_digest", "hash")
    if digest is None:
        raise EvidenceFormatError("page evidence requires digest/checksum")
    size = first("page_size", "size", "length")
    start = first("start", "offset", "address")
    end = first("end", "stop")
    page = first("page", "number", "page_number")
    algorithm = first("algorithm", "algo", "hash_algorithm")
    return PageChecksum(
        expected_digest=digest,
        algorithm=algorithm or "sha256",
        source=data.get("source"),
        note=data.get("note"),
        page=page,
        page_size=size,
        start=start,
        end=end,
    )
