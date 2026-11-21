"""Symbol tables: resolving a module-relative address to an inline chain.

Lookups are exact and half-open: an address belongs to a function when
``function.lo <= address < function.hi``.  Because :mod:`cpu_sampler.model`
already rejects overlapping siblings, a single binary search per level is
enough and the result is unambiguous.

Start offsets are precomputed once, while the immutable trees are being
validated, and stored on each node (``function_starts`` / ``inline_starts``
/ ``child_starts``); lookups therefore never rebuild key lists.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Dict, Optional, Sequence, Tuple

from .errors import DuplicateImageError, UnknownImageError
from .model import FunctionRange, InlineFrame, ModuleImage, SymbolMatch

__all__ = ["SymbolTable"]


def _locate(nodes: Sequence, starts: Sequence[int], address: int):
    """Return the node whose ``[lo, hi)`` contains ``address``, else ``None``.

    ``nodes`` is sorted by ``lo`` and free of overlaps, and ``starts`` holds
    the matching ``lo`` values in the same order.
    """

    if not nodes:
        return None
    index = bisect_right(starts, address) - 1
    if index < 0:
        return None
    candidate = nodes[index]
    if candidate.lo <= address < candidate.hi:
        return candidate
    return None


def _inline_chain(
    function: FunctionRange, relative_address: int
) -> Tuple[str, ...]:
    """Walk nested inline instances, outermost first."""

    chain = [function.name]
    nodes = function.inlines
    starts = function.inline_starts
    while nodes:
        match = _locate(nodes, starts, relative_address)
        if match is None:
            break
        assert isinstance(match, InlineFrame)
        chain.append(match.name)
        nodes = match.children
        starts = match.child_starts
    return tuple(chain)


class SymbolTable:
    """A collection of :class:`ModuleImage` objects keyed by module/version."""

    def __init__(self, images=()) -> None:
        self._images: Dict[Tuple[str, str], ModuleImage] = {}
        for image in images:
            self.add(image)

    # ------------------------------------------------------------------ setup
    def add(self, image: ModuleImage) -> None:
        """Register ``image``; a second image with the same key is an error."""

        if not isinstance(image, ModuleImage):
            raise TypeError(
                f"image must be a ModuleImage, got {type(image).__name__}"
            )
        if image.key in self._images:
            raise DuplicateImageError(
                f"an image for {image.describe()} is already registered"
            )
        self._images[image.key] = image

    def get(self, module: str, version: str) -> Optional[ModuleImage]:
        return self._images.get((module, version))

    def require(self, module: str, version: str) -> ModuleImage:
        image = self.get(module, version)
        if image is None:
            raise UnknownImageError(f"no image supplied for {module}@{version}")
        return image

    def __contains__(self, key: object) -> bool:
        return key in self._images

    def __len__(self) -> int:
        return len(self._images)

    @property
    def images(self) -> Tuple[ModuleImage, ...]:
        return tuple(self._images.values())

    # ---------------------------------------------------------------- queries
    def resolve(
        self, module: str, version: str, relative_address: int
    ) -> Optional[SymbolMatch]:
        """Resolve ``relative_address`` inside ``module@version``.

        Returns ``None`` when the address falls in a hole between functions
        (or outside the image); that is a legitimate "no symbol" answer, not
        an error.  An unknown module/version *is* an error.
        """

        image = self.require(module, version)
        if (
            isinstance(relative_address, bool)
            or not isinstance(relative_address, int)
            or relative_address < 0
            or relative_address >= image.size
        ):
            return None

        function = _locate(
            image.functions, image.function_starts, relative_address
        )
        if not isinstance(function, FunctionRange):
            return None

        return SymbolMatch(
            module=image.module,
            version=image.version,
            relative_address=relative_address,
            function=function.name,
            chain=_inline_chain(function, relative_address),
            build_id=image.build_id,
        )
