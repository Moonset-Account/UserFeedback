"""Independent checks for an exported document.

The exporter is the thing under test, so these helpers re-read the *serialized*
form and re-derive everything from scratch: object closure, page roots, the
absence of private pages, and the text/speech that a reader would extract.
"""

from __future__ import annotations

from typing import Any, Mapping

from .errors import DocError
from .model import Document


def verify_export(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an exported document and return what can be re-extracted.

    Raises :class:`~docredact.errors.DocError` when the export is not closed
    under public reachability, still contains a private page, or carries an
    object that no public page can reach.
    """

    document = Document.from_dict(data)
    private_pages = sorted(
        page for page, record in document.pages.items() if not record.get("public", True)
    )
    if private_pages:
        raise DocError(f"export contains private pages: {private_pages}")

    reachable = document.reachable_objects(public_only=True)
    serialized = set(document.objects)
    missing = serialized - reachable
    if missing:
        raise DocError(f"export contains unreachable objects: {sorted(missing)}")
    dangling = reachable - serialized
    if dangling:  # pragma: no cover - Document.from_dict already rejects this
        raise DocError(f"export is missing reachable objects: {sorted(dangling)}")

    return {
        "pages": sorted(document.pages),
        "objects": sorted(serialized),
        "text": {page: document.page_text(page) for page in sorted(document.pages)},
        "speech": {page: document.page_speech(page) for page in sorted(document.pages)},
    }


__all__ = ["verify_export"]
