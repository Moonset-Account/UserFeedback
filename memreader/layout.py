"""Structure layout descriptors.

A structure is an ordered tuple of fields.  Supported field kinds:

* ``UIntField``   -- fixed-width integer (1/2/4/8 bytes)
* ``BytesField``  -- fixed-length byte string
* ``PointerField``-- pointer-sized unsigned integer (4 or 8 bytes)
* ``ArrayField``  -- array whose element count is taken from the already
  parsed integer field named by ``count_from``; elements are fixed-size
  (``UIntField`` or ``BytesField``)

Fields are laid out back to back from the object's base VA; a read may
span any number of contiguous fragments.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UIntField:
    name: str
    size: int
    byteorder: str = "little"
    signed: bool = False

    def __post_init__(self):
        if self.size not in (1, 2, 4, 8):
            raise ValueError(f"UIntField {self.name}: size must be 1/2/4/8")
        if self.byteorder not in ("little", "big"):
            raise ValueError(f"UIntField {self.name}: bad byteorder")


@dataclass(frozen=True)
class BytesField:
    name: str
    length: int

    def __post_init__(self):
        if self.length <= 0:
            raise ValueError(f"BytesField {self.name}: length must be > 0")


@dataclass(frozen=True)
class PointerField:
    name: str
    size: int = 8
    byteorder: str = "little"

    def __post_init__(self):
        if self.size not in (4, 8):
            raise ValueError(f"PointerField {self.name}: size must be 4 or 8")
        if self.byteorder not in ("little", "big"):
            raise ValueError(f"PointerField {self.name}: bad byteorder")


@dataclass(frozen=True)
class ArrayField:
    name: str
    element: UIntField | BytesField
    count_from: str

    def __post_init__(self):
        if not isinstance(self.element, (UIntField, BytesField)):
            raise ValueError(
                f"ArrayField {self.name}: element must be UIntField or BytesField"
            )


@dataclass(frozen=True)
class StructDesc:
    name: str
    fields: tuple

    def __post_init__(self):
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError(f"StructDesc {self.name}: duplicate field names")
        for f in self.fields:
            if isinstance(f, ArrayField) and f.count_from not in names:
                raise ValueError(
                    f"StructDesc {self.name}: array {f.name} counts from "
                    f"unknown field {f.count_from}"
                )
