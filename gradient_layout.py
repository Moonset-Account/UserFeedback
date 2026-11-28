"""Gradient layout compiler and bidirectional name<->offset mapping.

The optimizer side of a training runtime sees one fixed-length flat byte
segment per rank.  The model side addresses gradients by parameter name and
logical (multi-dimensional) indices.  This module compiles a static layout
from parameter declarations and provides the two directions of the mapping:

* ``gather``  -- read a rectangular logical slice of a parameter out of the
  per-rank flat buffers.
* ``scatter`` -- write an update for a rectangular logical slice back into
  the per-rank flat buffers.

Layout rules
------------
* Only trainable (non-frozen) parameters occupy the buffer, in declaration
  order.
* Every parameter starts at a byte offset that is a multiple of ``alignment``;
  the gap left by alignment is zero padding.
* After the last parameter the buffer is padded with zeros up to a multiple
  of ``lcm(alignment, num_ranks)`` so that every rank owns exactly
  ``total_bytes // num_ranks`` bytes and rank boundaries stay aligned.
* A parameter may span two or more ranks; a single element may even straddle
  a rank boundary.  Logical rows of a multi-dimensional parameter are in
  general *not* contiguous in the flat buffer once slicing is involved.
* Padding bytes are never read or written by ``gather``/``scatter``.

The logical mapping (parameter name + indices -> absolute byte offset) does
not depend on ``num_ranks``; only the partition of the flat buffer into
per-rank segments does.  Rebuilding the parameters from the same set of
logical updates therefore yields element-wise identical results for any
rank count.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from itertools import product
from typing import Iterable, Sequence

__all__ = [
    "LayoutError",
    "ParamSpec",
    "ParamLayout",
    "GradientLayout",
]

_FLOAT_FORMATS = {2: "e", 4: "f", 8: "d"}
_INT_FORMATS = {1: "b", 2: "h", 4: "i", 8: "q"}
_UINT_FORMATS = {1: "B", 2: "H", 4: "I", 8: "Q"}
_KIND_TABLES = {"float": _FLOAT_FORMATS, "int": _INT_FORMATS, "uint": _UINT_FORMATS}


class LayoutError(ValueError):
    """Invalid layout declaration, buffer shape, or update payload."""


def _struct_format(width: int, kind: str) -> str:
    try:
        return _KIND_TABLES[kind][width]
    except KeyError:
        raise LayoutError(
            f"unsupported element: kind={kind!r} width={width!r}"
        ) from None


def _align_up(value: int, quantum: int) -> int:
    return -(-value // quantum) * quantum


@dataclass(frozen=True)
class ParamSpec:
    """Declaration of one model parameter.

    ``shape``  -- logical shape, at least one dimension, all dims >= 1.
    ``width``  -- element width in bytes.
    ``frozen`` -- frozen parameters are excluded from the gradient buffer.
    ``kind``   -- value interpretation: "float" (default), "int" or "uint".
    """

    name: str
    shape: tuple[int, ...]
    width: int = 4
    frozen: bool = False
    kind: str = "float"


@dataclass(frozen=True)
class ParamLayout:
    """Compiled placement of one trainable parameter."""

    name: str
    shape: tuple[int, ...]
    width: int
    kind: str
    fmt: str
    offset: int  # absolute byte offset in the flat (all-rank) buffer
    numel: int
    nbytes: int
    strides: tuple[int, ...]  # row-major element strides

    @property
    def byte_span(self) -> tuple[int, int]:
        return (self.offset, self.offset + self.nbytes)


class GradientLayout:
    """Compiled layout plus the bidirectional name<->offset mapping."""

    def __init__(
        self,
        specs: Iterable[ParamSpec],
        alignment: int = 64,
        num_ranks: int = 1,
    ) -> None:
        if not isinstance(alignment, int) or alignment < 1:
            raise LayoutError("alignment must be a positive integer")
        if not isinstance(num_ranks, int) or num_ranks < 1:
            raise LayoutError("num_ranks must be a positive integer")
        self.alignment = alignment
        self.num_ranks = num_ranks

        params: dict[str, ParamLayout] = {}
        offset = 0
        for spec in specs:
            self._validate_spec(spec)
            if spec.frozen:
                continue
            if spec.name in params:
                raise LayoutError(f"duplicate parameter name: {spec.name!r}")
            offset = _align_up(offset, alignment)
            numel = math.prod(spec.shape)
            strides = self._row_major_strides(spec.shape)
            layout = ParamLayout(
                name=spec.name,
                shape=tuple(spec.shape),
                width=spec.width,
                kind=spec.kind,
                fmt=_struct_format(spec.width, spec.kind),
                offset=offset,
                numel=numel,
                nbytes=numel * spec.width,
                strides=strides,
            )
            params[spec.name] = layout
            offset += layout.nbytes

        self.params = params
        # Pad so the buffer is divisible by both alignment and num_ranks;
        # then every rank segment has equal, alignment-multiple length.
        quantum = math.lcm(alignment, num_ranks)
        self.total_bytes = _align_up(offset, quantum)
        self.rank_bytes = self.total_bytes // num_ranks

    # ------------------------------------------------------------------
    # declaration validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_spec(spec: ParamSpec) -> None:
        if not isinstance(spec.name, str) or not spec.name:
            raise LayoutError("parameter name must be a non-empty string")
        if (
            not isinstance(spec.shape, tuple)
            or not spec.shape
            or any(not isinstance(d, int) or isinstance(d, bool) or d < 1 for d in spec.shape)
        ):
            raise LayoutError(
                f"parameter {spec.name!r}: shape must be a non-empty tuple of "
                "positive integers"
            )
        if not isinstance(spec.width, int) or spec.width < 1:
            raise LayoutError(f"parameter {spec.name!r}: width must be >= 1")
        _struct_format(spec.width, spec.kind)  # validates kind/width combo

    @staticmethod
    def _row_major_strides(shape: tuple[int, ...]) -> tuple[int, ...]:
        strides = []
        acc = 1
        for dim in reversed(shape):
            strides.append(acc)
            acc *= dim
        return tuple(reversed(strides))

    # ------------------------------------------------------------------
    # buffer helpers
    # ------------------------------------------------------------------
    def new_buffers(self) -> list[bytearray]:
        """Allocate zeroed per-rank buffers for this layout."""
        return [bytearray(self.rank_bytes) for _ in range(self.num_ranks)]

    def padding_spans(self) -> list[tuple[int, int]]:
        """Byte ranges of the flat buffer that are pure padding."""
        spans = []
        cursor = 0
        for layout in self.params.values():
            if layout.offset > cursor:
                spans.append((cursor, layout.offset))
            cursor = layout.offset + layout.nbytes
        if self.total_bytes > cursor:
            spans.append((cursor, self.total_bytes))
        return spans

    def _check_buffers(self, buffers: Sequence, mutable: bool) -> None:
        if len(buffers) != self.num_ranks:
            raise LayoutError(
                f"expected {self.num_ranks} rank buffers, got {len(buffers)}"
            )
        for i, buf in enumerate(buffers):
            if len(buf) != self.rank_bytes:
                raise LayoutError(
                    f"rank {i} buffer has {len(buf)} bytes, "
                    f"expected {self.rank_bytes}"
                )
            if mutable:
                if isinstance(buf, memoryview):
                    if buf.readonly:
                        raise LayoutError(f"rank {i} buffer is read-only")
                elif not isinstance(buf, bytearray):
                    raise LayoutError(
                        "scatter requires mutable per-rank buffers "
                        "(bytearray or writable memoryview)"
                    )

    def _read_bytes(self, buffers: Sequence, offset: int, n: int) -> bytes:
        rank, local = divmod(offset, self.rank_bytes)
        if local + n <= self.rank_bytes:
            return bytes(buffers[rank][local : local + n])
        first = self.rank_bytes - local
        return bytes(buffers[rank][local:]) + bytes(buffers[rank + 1][: n - first])

    def _write_bytes(self, buffers: Sequence, offset: int, data: bytes) -> None:
        rank, local = divmod(offset, self.rank_bytes)
        n = len(data)
        if local + n <= self.rank_bytes:
            buffers[rank][local : local + n] = data
        else:
            first = self.rank_bytes - local
            buffers[rank][local:] = data[:first]
            buffers[rank + 1][: n - first] = data[first:]

    # ------------------------------------------------------------------
    # logical selection -> absolute byte offsets
    # ------------------------------------------------------------------
    def _layout_of(self, name: str) -> ParamLayout:
        try:
            return self.params[name]
        except KeyError:
            raise KeyError(f"no trainable parameter named {name!r}") from None

    @staticmethod
    def _bound(value: int | None, size: int, default: int, what: str) -> int:
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"slice {what} must be an integer, got {value!r}")
        if value < 0:
            value += size
        if not 0 <= value <= size:
            raise IndexError(
                f"slice {what} {value} out of bounds for dimension of size {size}"
            )
        return value

    def _normalize_selection(
        self, layout: ParamLayout, selection
    ) -> tuple[list[list[int]], tuple[int, ...]]:
        ndim = len(layout.shape)
        if selection is None:
            selection = ()
        elif not isinstance(selection, tuple):
            selection = (selection,)
        if len(selection) > ndim:
            raise IndexError(
                f"too many indices for {layout.name!r}: "
                f"got {len(selection)}, parameter has {ndim} dimensions"
            )
        selection = selection + (slice(None),) * (ndim - len(selection))

        dims: list[list[int]] = []
        out_shape: list[int] = []
        for sel, size in zip(selection, layout.shape):
            if isinstance(sel, bool):
                raise TypeError("boolean indices are not supported")
            if isinstance(sel, int):
                idx = sel + size if sel < 0 else sel
                if not 0 <= idx < size:
                    raise IndexError(
                        f"index {sel} out of bounds for dimension of size {size}"
                    )
                dims.append([idx])  # integer index drops the dimension
            elif isinstance(sel, slice):
                step = 1 if sel.step is None else sel.step
                if step != 1:
                    raise IndexError(
                        "only unit-step rectangular slices are supported"
                    )
                start = self._bound(sel.start, size, 0, "start")
                stop = self._bound(sel.stop, size, size, "stop")
                if start > stop:
                    raise IndexError(
                        f"slice start {start} exceeds slice stop {stop}"
                    )
                dims.append(list(range(start, stop)))
                out_shape.append(stop - start)
            else:
                raise TypeError(f"unsupported index component: {sel!r}")
        return dims, tuple(out_shape)

    @staticmethod
    def _flat_index(layout: ParamLayout, coord: tuple[int, ...]) -> int:
        return sum(c * s for c, s in zip(coord, layout.strides))

    # ------------------------------------------------------------------
    # gather / scatter
    # ------------------------------------------------------------------
    def gather(self, buffers: Sequence, name: str, selection=None):
        """Read a rectangular logical slice of ``name`` from ``buffers``.

        ``selection`` is an int/slice or a tuple of them (missing trailing
        dimensions select everything).  Returns nested lists matching the
        sliced shape; an all-integer selection returns a scalar.
        """
        layout = self._layout_of(name)
        self._check_buffers(buffers, mutable=False)
        dims, out_shape = self._normalize_selection(layout, selection)
        fmt = "<" + layout.fmt
        flat = []
        for coord in product(*dims):
            offset = layout.offset + self._flat_index(layout, coord) * layout.width
            raw = self._read_bytes(buffers, offset, layout.width)
            flat.append(struct.unpack(fmt, raw)[0])
        return _reshape(flat, out_shape)

    def scatter(self, buffers: Sequence, name: str, values, selection=None) -> None:
        """Write ``values`` into the slice of ``name`` given by ``selection``.

        Only bytes belonging to the selected elements are touched; padding
        and neighbouring parameters are left unchanged.  ``values`` may be a
        flat iterable or nested lists matching the sliced shape.
        """
        layout = self._layout_of(name)
        self._check_buffers(buffers, mutable=True)
        dims, out_shape = self._normalize_selection(layout, selection)
        flat_values = _flatten(values)
        expected = math.prod(out_shape) if out_shape else 1
        if len(flat_values) != expected:
            raise LayoutError(
                f"update for {name!r} has {len(flat_values)} values, "
                f"slice expects {expected}"
            )
        fmt = "<" + layout.fmt
        for coord, value in zip(product(*dims), flat_values):
            offset = layout.offset + self._flat_index(layout, coord) * layout.width
            self._write_bytes(buffers, offset, struct.pack(fmt, value))

    # ------------------------------------------------------------------
    # convenience drivers
    # ------------------------------------------------------------------
    def apply_updates(self, updates: Iterable, buffers: Sequence | None = None):
        """Apply ``(name, values)`` or ``(name, selection, values)`` updates.

        Returns the buffers (freshly allocated zeroed ones if not given).
        """
        if buffers is None:
            buffers = self.new_buffers()
        for update in updates:
            if len(update) == 2:
                name, values = update
                selection = None
            elif len(update) == 3:
                name, selection, values = update
            else:
                raise LayoutError(
                    "updates must be (name, values) or (name, selection, values)"
                )
            self.scatter(buffers, name, values, selection)
        return buffers

    def rebuild(self, buffers: Sequence) -> dict[str, object]:
        """Gather every trainable parameter in full, keyed by name."""
        return {name: self.gather(buffers, name) for name in self.params}


def _flatten(values) -> list:
    if isinstance(values, (list, tuple)):
        out: list = []
        for item in values:
            out.extend(_flatten(item))
        return out
    return [values]


def _reshape(flat: list, shape: tuple[int, ...]):
    if not shape:
        return flat[0]
    if len(shape) == 1:
        return list(flat[: shape[0]])
    step = math.prod(shape[1:])
    return [
        _reshape(flat[i * step : (i + 1) * step], shape[1:])
        for i in range(shape[0])
    ]
