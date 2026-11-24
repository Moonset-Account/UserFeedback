"""Canonical natural-person resolution across identity directories."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .models import Identity, NaturalPerson


class IdentityResolutionError(ValueError):
    """Base class for identity resolution failures."""


class UnresolvedIdentityError(IdentityResolutionError):
    """Raised in strict mode when an identity has no natural-person link."""


class IdentityConflictError(IdentityResolutionError):
    """Raised when one identity is linked to two different natural persons."""


class PersonRegistry:
    """Map directory identities to canonical natural-person IDs."""

    def __init__(self, links: Iterable[Any] | Mapping[str, str] | None = None) -> None:
        self._identity_to_person: dict[str, str] = {}
        self._person_to_identities: dict[str, set[Identity]] = defaultdict(set)
        if links is not None:
            if isinstance(links, Mapping):
                for identity, person_id in links.items():
                    self.bind(person_id, Identity.parse(identity))
            else:
                for link in links:
                    self.add(link)

    def add(self, link: Any) -> "PersonRegistry":
        """Add one of the accepted link shapes.

        Accepted shapes are:

        * :class:`NaturalPerson`
        * ``(person_id, directory, subject)``
        * ``{"person_id": ..., "directory": ..., "subject": ...}``
        * ``{"person_id": ..., "identities": [Identity/dict/string, ...]}``
        """

        if isinstance(link, NaturalPerson):
            for identity in link.identities:
                self.bind(link.person_id, identity)
            return self

        if isinstance(link, tuple) and len(link) == 3:
            person_id, directory, subject = link
            self.bind(str(person_id), Identity(str(directory), str(subject)))
            return self

        if isinstance(link, Mapping):
            person_id = link.get("person_id") or link.get("id") or link.get("natural_person_id")
            if not person_id:
                raise ValueError(f"identity link requires person_id: {link!r}")

            if "identities" in link:
                raw_identities = link["identities"]
                if not isinstance(raw_identities, Iterable) or isinstance(
                    raw_identities, (str, bytes)
                ):
                    raise ValueError("identities must be an iterable")
                for raw_identity in raw_identities:
                    self.bind(str(person_id), Identity.parse(raw_identity))

            if "identity" in link:
                self.bind(str(person_id), Identity.parse(link["identity"]))

            if "directory" in link and "subject" in link:
                self.bind(
                    str(person_id),
                    Identity(str(link["directory"]), str(link["subject"])),
                )
            elif "directory" in link and ("id" in link or "username" in link or "email" in link):
                subject = link.get("subject") or link.get("id") or link.get("username") or link.get("email")
                self.bind(str(person_id), Identity(str(link["directory"]), str(subject)))

            if not any(
                key in link for key in ("identities", "identity", "directory")
            ):
                raise ValueError(f"identity link contains no identity: {link!r}")
            return self

        raise TypeError(f"unsupported identity link: {link!r}")

    def bind(self, person_id: str, identity: Identity | dict[str, Any] | str) -> "PersonRegistry":
        """Bind one identity to a canonical person."""

        canonical_person = str(person_id).strip()
        if not canonical_person:
            raise ValueError("person_id is required")
        parsed = Identity.parse(identity)
        existing = self._identity_to_person.get(parsed.urn)
        if existing is not None and existing != canonical_person:
            raise IdentityConflictError(
                f"identity {parsed.urn!r} is linked to both {existing!r} and {canonical_person!r}"
            )
        self._identity_to_person[parsed.urn] = canonical_person
        self._person_to_identities[canonical_person].add(parsed)
        return self

    def person_for(self, identity: Identity | dict[str, Any] | str) -> str:
        parsed = Identity.parse(identity)
        try:
            return self._identity_to_person[parsed.urn]
        except KeyError as exc:
            raise UnresolvedIdentityError(
                f"no natural-person link for identity {parsed.urn!r}"
            ) from exc

    def identities_for(self, person_id: str) -> tuple[Identity, ...]:
        return tuple(sorted(self._person_to_identities.get(str(person_id), set()), key=str))

    def is_known(self, identity: Identity | dict[str, Any] | str) -> bool:
        return Identity.parse(identity).urn in self._identity_to_person

    def __contains__(self, identity: object) -> bool:
        if isinstance(identity, (Identity, dict, str)):
            try:
                return self.is_known(identity)
            except (TypeError, ValueError):
                return False
        return False
