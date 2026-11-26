"""Field dictionary loading and private-attribute approval.

The dictionary is intentionally tolerant of the shapes a synthetic
exercise might use.  It can be loaded from a JSON file or built from an
ordinary ``dict`` / ``list`` and understands:

* tag keys (``"0020000D"``, ``"0020,000D"``, ``"(0020,000D)"``);
* DICOM keywords and snake/camel case variants;
* VR labels ``"UI"`` and the prose label ``"UID"``;
* entries given as a VR string or as an object;
* explicit private markers and approval lists.

A private attribute is detected either from an odd DICOM group number or
from an explicit ``"private": true`` dictionary entry.  It is retained
only when the dictionary approves it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union

# DICOM VRs (PS3.5) plus the prose aliases "UID" / "SEQUENCE".
UID_VRS: Set[str] = {"UI", "UID"}
SEQUENCE_VRS: Set[str] = {"SQ", "SEQUENCE"}
KNOWN_VRS: Set[str] = {
    "AE", "AS", "AT", "CS", "DA", "DS", "DT", "FD", "FL", "IS", "LO",
    "LT", "OB", "OD", "OF", "OL", "OV", "OW", "PN", "SH", "SL", "SQ",
    "SS", "ST", "TM", "UC", "UI", "UL", "UN", "UR", "US", "UT", "UV",
    "SV",
}

# Common registered transfer syntax UIDs - these literally encode how
# the data stream is encoded/compressed and must pass through unchanged.
# The set is illustrative of the registry rather than a full copy of
# PS3.6; callers can add values via the dictionary / gateway.
TRANSFER_SYNTAX_UIDS: Set[str] = {
    "1.2.840.10008.1.2",       # Implicit VR Little Endian
    "1.2.840.10008.1.2.0.99",  # Deflated Explicit VR Little Endian
    "1.2.840.10008.1.2.1",     # Explicit VR Little Endian
    "1.2.840.10008.1.2.1.99",  # Deflated Explicit VR Little Endian (?)
    "1.2.840.10008.1.2.2",     # Explicit VR Big Endian (retired)
    "1.2.840.10008.1.2.4.50",  # JPEG Baseline (Process 1)
    "1.2.840.10008.1.2.4.51",  # JPEG Extended (Process 2 & 4)
    "1.2.840.10008.1.2.4.52",  # JPEG Lossless Non-Hierarchical (Process 14)
    "1.2.840.10008.1.2.4.53",  # JPEG Lossless Selection Value 1
    "1.2.840.10008.1.2.4.54",  # JPEG Lossless Selection Value 1
    "1.2.840.10008.1.2.4.55",  # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.57",  # JPEG-LS Lossy (Near-Lossless)
    "1.2.840.10008.1.2.4.70",  # JPEG Lossless Selection Value 1
    "1.2.840.10008.1.2.4.80",  # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.81",  # JPEG-LS Lossy
    "1.2.840.10008.1.2.4.90",  # JPEG 2000 Lossless
    "1.2.840.10008.1.2.4.91",  # JPEG 2000
    "1.2.840.10008.1.2.4.92",  # JPEG 2000 Part 2 Lossless
    "1.2.840.10008.1.2.4.93",  # JPEG 2000 Part 2
    "1.2.840.10008.1.2.4.94",  # JPIP Referenced
    "1.2.840.10008.1.2.4.95",  # JPIP Referenced Deflate
    "1.2.840.10008.1.2.4.100", # MPEG2 Main Profile @ Main Level
    "1.2.840.10008.1.2.4.101", # MPEG2 Main Profile @ High Level
    "1.2.840.10008.1.2.4.102", # MPEG-4 AVC/H.264 High Profile / Level 4.1
    "1.2.840.10008.1.2.4.103", # MPEG-4 AVC/H.264 BD-compatible
    "1.2.840.10008.1.2.4.104", # MPEG-4 AVC/H.264 High Profile / Level 4.2
    "1.2.840.10008.1.2.4.105", # MPEG-4 AVC/H.264 High Profile / Level 4.2
    "1.2.840.10008.1.2.4.106", # HEVC/H.265 Main Profile / Level 5.1
    "1.2.840.10008.1.2.4.107", # HEVC/H.265 Main 10 Profile / Level 5.1
    "1.2.840.10008.1.2.5",     # RLE Lossless
    "1.2.840.10008.1.2.6.1",   # RFC 2557 MIME Encapsulation
    "1.2.840.10008.1.2.1.98",  # Encapsulated Uncompressed Explicit VR LE
    "1.2.840.10008.1.2.20",    # XML Encoding
}

# Vocabulary used in dictionary entries to mark a field as a registered
# encoding/constant UID rather than an instance identifier/reference.
CONSTANT_ROLES = {
    "constant", "constants", "registered", "registered_constant",
    "encoding", "encoding_constant", "transfer_syntax", "preserve",
    "keep", "literal", "code", "coding",
}
IDENTIFIER_ROLES = {
    "id", "uid", "identifier", "instance", "sop", "study", "series",
    "reference", "ref", "referenced", "foreign_key", "fk",
}


def normalize_tag(key: object) -> Optional[str]:
    """Return canonical ``GGGGEEEE`` upper-case for *key*, else ``None``."""

    if not isinstance(key, str):
        return None
    text = key.strip()
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    compact = text.replace(",", "").replace(" ", "").replace("\t", "")
    if re.fullmatch(r"[0-9A-Fa-f]{8}", compact):
        return compact.upper()
    return None


def is_private_tag(tag8: str) -> bool:
    """DICOM private data elements have an odd group number."""

    return (int(tag8[:4], 16) & 0x0001) == 1


def normalize_vr(value: object) -> Optional[str]:
    """Normalize a VR/type label to a DICOM VR token (``UI``/``SQ``/...)."""

    if not isinstance(value, str):
        return None
    token = value.strip().upper().replace("-", "_")
    aliases = {
        "UID": "UI",
        "UIDREF": "UI",
        "UNIQUE_IDENTIFIER": "UI",
        "SEQUENCE": "SQ",
        "SEQ": "SQ",
    }
    token = aliases.get(token, token)
    if token in KNOWN_VRS:
        return token
    return None


def _keyword_key(key: str) -> str:
    return re.sub(r"[_\-\s]", "", key).lower()


@dataclass
class FieldEntry:
    """Normalized dictionary entry for one attribute."""

    key: str
    tag: Optional[str] = None
    keyword: Optional[str] = None
    vr: Optional[str] = None
    role: Optional[str] = None
    private: Optional[bool] = None
    approved: bool = False
    constants: Set[str] = field(default_factory=set)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_uid(self) -> bool:
        if self.vr in UID_VRS:
            return True
        # An explicit non UID VR wins over a broad semantic role.
        if self.vr is not None:
            return False
        # A dictionary may classify a field semantically without a VR.
        return (self.role or "").strip().lower() in IDENTIFIER_ROLES

    @property
    def is_sequence(self) -> bool:
        return self.vr in SEQUENCE_VRS

    @property
    def is_constant_field(self) -> bool:
        role = (self.role or "").strip().lower().replace("-", "_")
        return role in CONSTANT_ROLES or bool(self.raw.get("constant")) or bool(
            self.raw.get("preserve")
        )


class FieldDictionary:
    """Lookup and approval rules backed by a normalized field dictionary."""

    def __init__(
        self,
        entries: Optional[Iterable[Union[FieldEntry, Dict[str, Any]]]] = None,
        *,
        approved_private: Optional[Iterable[str]] = None,
        registered_constants: Optional[Iterable[str]] = None,
        preserve_transfer_syntaxes: bool = True,
    ) -> None:
        self._by_tag: Dict[str, FieldEntry] = {}
        self._by_keyword: Dict[str, FieldEntry] = {}
        self.approved_private: Set[str] = set()
        self.registered_constants: Set[str] = set(registered_constants or ())
        if preserve_transfer_syntaxes:
            self.registered_constants |= TRANSFER_SYNTAX_UIDS

        for item in approved_private or ():
            self.approve_private_value(item)

        for item in entries or ():
            entry = item if isinstance(item, FieldEntry) else self._normalize_entry(item)
            self.add(entry)

    # ------------------------------------------------------------ construction
    @classmethod
    def load(cls, path: Union[str, Path]) -> "FieldDictionary":
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: Union[None, dict, list, str, Path, "FieldDictionary"]) -> "FieldDictionary":
        """Build a dictionary from many plausible JSON shapes."""

        if data is None:
            return cls()
        if isinstance(data, FieldDictionary):
            return data
        if isinstance(data, str) and data.lstrip()[:1] in {"{", "["}:
            return cls.from_dict(json.loads(data))
        if isinstance(data, (str, Path)):
            path = Path(data)
            if path.exists():
                return cls.load(path)
            raise FileNotFoundError(f"dictionary file not found: {data}")
        if isinstance(data, list):
            return cls(entries=[cls._coerce_entry_raw(item) for item in data])
        if not isinstance(data, dict):
            raise TypeError(f"unsupported dictionary type: {type(data)!r}")

        # Top-level registered constants under any common name.
        constants: Set[str] = set()
        for name in (
            "registered_constants", "encoding_constants", "constants",
            "preserved_uids", "keep_uids",
        ):
            value = data.get(name)
            if isinstance(value, (list, tuple, set)):
                constants.update(str(v) for v in value)

        approved: List[str] = []
        for name in (
            "approved_private", "approved_private_attributes",
            "approved_private_tags", "allowed_private",
            "allowed_private_attributes", "private_approved",
            "private_whitelist", "whitelist",
        ):
            value = data.get(name)
            approved.extend(cls._iter_approval_items(value))

        preserve_ts = data.get("preserve_transfer_syntaxes", True)
        dictionary = cls(
            approved_private=approved,
            registered_constants=constants,
            preserve_transfer_syntaxes=bool(preserve_ts),
        )

        # Optional nested "private": {"Creator": {"approved": true,...}}
        private_section = data.get("private")
        if isinstance(private_section, dict):
            for key, spec in private_section.items():
                if isinstance(spec, dict) and spec.get("approved"):
                    dictionary.approve_private_value(key)
                    dictionary.add(dictionary._normalize_entry({**spec, "key": key, "private": True}))

        fields = data.get("fields", data.get("attributes", data.get("tags")))
        if fields is None:
            # The whole object may itself be the tag/keyword -> spec map,
            # apart from known meta keys.
            meta_keys = {
                "version", "format", "name", "description",
                "registered_constants", "encoding_constants", "constants",
                "preserved_uids", "keep_uids", "approved_private",
                "approved_private_attributes", "approved_private_tags",
                "allowed_private", "allowed_private_attributes",
                "private_approved", "private_whitelist", "whitelist",
                "preserve_transfer_syntaxes", "private",
                "fields", "attributes", "tags",
            }
            fields = {k: v for k, v in data.items() if k not in meta_keys}
        if isinstance(fields, dict):
            for key, spec in fields.items():
                raw = cls._coerce_entry_raw(spec, default_key=key)
                dictionary.add(dictionary._normalize_entry(raw))
        elif isinstance(fields, list):
            for spec in fields:
                dictionary.add(dictionary._normalize_entry(cls._coerce_entry_raw(spec)))
        return dictionary

    @staticmethod
    def _iter_approval_items(value: Any) -> Iterable[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        items: List[str] = []
        if isinstance(value, dict):
            for key, spec in value.items():
                if spec is True:
                    items.append(str(key))
                elif isinstance(spec, dict):
                    if spec.get("approved", True):
                        items.append(str(key))
                        if spec.get("tag"):
                            items.append(str(spec["tag"]))
        else:
            for item in value:
                if isinstance(item, str):
                    items.append(item)
                elif isinstance(item, dict):
                    if item.get("approved", True):
                        for name in ("key", "keyword", "tag", "name"):
                            if item.get(name):
                                items.append(str(item[name]))
        return items

    @staticmethod
    def _coerce_entry_raw(spec: Any, *, default_key: Optional[str] = None) -> Dict[str, Any]:
        if isinstance(spec, str):
            raw: Dict[str, Any] = {"vr": spec}
        elif spec is None:
            raw = {}
        elif isinstance(spec, FieldEntry):
            return spec.raw or {
                "key": spec.key, "tag": spec.tag, "keyword": spec.keyword,
                "vr": spec.vr, "role": spec.role, "private": spec.private,
                "approved": spec.approved,
            }
        elif isinstance(spec, dict):
            raw = dict(spec)
        else:
            raw = {"vr": str(spec)}
        if default_key is not None and not raw.get("key"):
            raw["key"] = default_key
        return raw

    def _normalize_entry(self, raw: Dict[str, Any]) -> FieldEntry:
        key_value = raw.get("key") or raw.get("keyword") or raw.get("name") or raw.get("tag")
        key = str(key_value) if key_value is not None else ""
        tag = normalize_tag(raw.get("tag")) or normalize_tag(key)
        keyword = raw.get("keyword") if isinstance(raw.get("keyword"), str) else None
        if not keyword and key and normalize_tag(key) is None:
            keyword = key
        vr = normalize_vr(
            raw.get("vr") or raw.get("VR") or raw.get("type")
            or raw.get("data_type") or raw.get("value_type")
        )
        role = raw.get("role") or raw.get("category") or raw.get("kind") or raw.get("class")
        if role is not None:
            role = str(role)
        private = raw.get("private")
        if private is None and tag is not None:
            private = is_private_tag(tag)
        # Accept common synonym spellings for explicit approval.
        approved = bool(raw.get("approved", False))
        for approval_alias in ("allowed", "permitted", "allow", "whitelisted"):
            if raw.get(approval_alias) is True:
                approved = True
        constants: Set[str] = set()
        for name in ("constants", "constant_values", "registered_values"):
            value = raw.get(name)
            if isinstance(value, (list, tuple, set)):
                constants.update(str(v) for v in value)
        entry = FieldEntry(
            key=key or (tag or ""),
            tag=tag,
            keyword=keyword,
            vr=vr,
            role=role,
            private=private if isinstance(private, bool) else None,
            approved=approved,
            constants=constants,
            raw=raw,
        )
        if approved:
            for candidate in (tag, keyword, key):
                if candidate:
                    self.approved_private.add(normalize_tag(candidate) or candidate)
        return entry

    def add(self, entry: FieldEntry) -> None:
        if entry.tag:
            self._by_tag[entry.tag] = entry
        if entry.keyword:
            self._by_keyword[_keyword_key(entry.keyword)] = entry
        if entry.key:
            tag = normalize_tag(entry.key)
            if tag:
                self._by_tag.setdefault(tag, entry)
            else:
                self._by_keyword.setdefault(_keyword_key(entry.key), entry)

    def approve_private_value(self, value: str) -> None:
        tag = normalize_tag(value)
        self.approved_private.add(tag or str(value))
        self.approved_private.add(_keyword_key(str(value)))

    # ---------------------------------------------------------------- lookup
    def lookup(self, key: object) -> Optional[FieldEntry]:
        if not isinstance(key, str):
            return None
        tag = normalize_tag(key)
        if tag and tag in self._by_tag:
            return self._by_tag[tag]
        return self._by_keyword.get(_keyword_key(key))

    def is_private(self, key: str, entry: Optional[FieldEntry] = None) -> bool:
        if entry is not None and entry.private is True:
            return True
        tag = normalize_tag(key)
        if tag is not None and is_private_tag(tag):
            return True
        return False

    def is_approved_private(self, key: str, entry: Optional[FieldEntry] = None) -> bool:
        if entry is not None and entry.approved:
            return True
        tag = normalize_tag(key)
        candidates = {key, _keyword_key(key)}
        if tag:
            candidates.add(tag)
        for candidate in candidates:
            if candidate in self.approved_private:
                return True
        return False

    def is_registered_constant_value(self, value: str) -> bool:
        return value in self.registered_constants
