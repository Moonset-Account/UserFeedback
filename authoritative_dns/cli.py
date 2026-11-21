"""Command-line entry point for the local authoritative kernel.

The CLI loads a zone from a small text file (see ``README.md``) and evaluates
one or more ``name type`` queries.  It never performs network I/O: every
answer comes from the in-memory fixture described by the zone file.

Examples
--------
    python -m authoritative_dns --zone zones/example.zone www.example. A
    python -m authoritative_dns --zone zones/example.zone --json \
        y.b.example. A

The module is also runnable as ``python authoritative_dns/__main__.py``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import List, Optional, Sequence, TextIO

from .records import RecordError
from .zone import Zone, ZoneError

EXIT_OK = 0
EXIT_INPUT_ERROR = 2

_A_OR_AAAA_OR_NS = ("A", "AAAA", "NS")
_SKIPPED_TYPES = ("SOA",)


def _strip_comment(line: str) -> str:
    """Remove a trailing ``;``/``#`` comment that is not inside quotes."""
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char in (";", "#"):
            return line[:index]
    return line


def load_zone(path: str, *, origin: Optional[str] = None) -> Zone:
    """Parse a zone file into an in-memory :class:`Zone`.

    File format (one record per line)::

        ; comment
        $ORIGIN example.
        $TTL 300
        @            IN  SOA    ns hostmaster 1 3600 600 86400 300
        @            IN  NS     ns1.example.
        @            IN  A      192.0.2.1
        www          IN  A      192.0.2.10
        *.example.   IN  TXT    "synthetic"

    Owner names may be absolute (trailing dot) or relative to ``$ORIGIN``.
    Directives other than ``$ORIGIN``/``$TTL``, the ``SOA`` record and types
    outside ``A``/``AAAA``/``TXT``/``NS`` are skipped, which keeps ordinary
    fixtures loadable.  TXT character-strings come from shell-style quoting,
    so ``"hello" "world"`` is two character-strings.
    """
    current_origin: Optional[str] = origin
    default_ttl = 300
    parsed_ttl = default_ttl
    pending: List[dict] = []

    with open(path, "r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            line = _strip_comment(raw_line).strip()
            if not line:
                continue
            try:
                tokens = shlex.split(line, comments=False)
            except ValueError as exc:
                raise ZoneError(f"{path}:{lineno}: {exc}") from exc
            if not tokens:
                continue
            keyword = tokens[0].upper()

            if keyword == "$ORIGIN":
                if len(tokens) != 2:
                    raise ZoneError(
                        f"{path}:{lineno}: $ORIGIN needs exactly one argument"
                    )
                current_origin = tokens[1]
                continue
            if keyword == "$TTL":
                if len(tokens) != 2 or not tokens[1].isdigit():
                    raise ZoneError(f"{path}:{lineno}: $TTL needs an integer")
                default_ttl = int(tokens[1])
                parsed_ttl = default_ttl
                continue

            if len(tokens) < 2:
                raise ZoneError(f"{path}:{lineno}: record needs owner and type")
            owner_token, rest = tokens[0], tokens[1:]
            if rest and rest[0].upper() in ("IN", "CH", "HS"):
                rest = rest[1:]
            if not rest:
                raise ZoneError(f"{path}:{lineno}: record needs a type")
            rtype = rest[0].upper()
            rest = rest[1:]

            if rest and rest[0].isdigit():
                ttl = int(rest[0])
                rest = rest[1:]
            else:
                ttl = default_ttl

            if rtype in _SKIPPED_TYPES:
                continue
            if rtype not in ("A", "AAAA", "TXT", "NS"):
                continue

            owner = _absolute_owner(owner_token, current_origin, path, lineno)
            if rtype == "TXT":
                if not rest:
                    raise ZoneError(f"{path}:{lineno}: TXT record needs data")
                value = tuple(rest)
            else:
                if not rest:
                    raise ZoneError(
                        f"{path}:{lineno}: {rtype} record needs data"
                    )
                value = " ".join(rest)

            pending.append(
                {"name": owner, "rtype": rtype, "value": value, "ttl": ttl}
            )

    if current_origin is not None:
        zone_origin = current_origin
    elif pending:
        zone_origin = pending[0]["name"]
    else:
        raise ZoneError(f"{path}: no $ORIGIN and no records to infer one")

    zone = Zone(zone_origin, default_ttl=parsed_ttl)
    for item in pending:
        zone.add(item["name"], item["rtype"], item["value"], ttl=item["ttl"])
    return zone


def _absolute_owner(
    token: str, current_origin: Optional[str], path: str, lineno: int
) -> str:
    if token == "@":
        if current_origin is None:
            raise ZoneError(f"{path}:{lineno}: '@' used before $ORIGIN")
        return current_origin
    if token.endswith("."):
        return token
    if current_origin is None:
        raise ZoneError(
            f"{path}:{lineno}: relative owner {token!r} needs an $ORIGIN"
        )
    return f"{token}.{current_origin}"


def _format_response(response, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(response.to_dict(), indent=2, sort_keys=True)

    lines = [f";; {response.qname} {response.qtype} -> {response.status}"]
    if response.source:
        suffix = " (wildcard)" if response.wildcard else ""
        lines.append(f";; source {response.source}{suffix}")
    for rr in response.records:
        lines.append(str(rr))
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m authoritative_dns",
        description=(
            "Answer A/AAAA/TXT queries from a local in-memory authoritative "
            "zone and return referrals at NS delegation points."
        ),
    )
    parser.add_argument("--zone", required=True, help="path to the zone fixture")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument(
        "queries",
        nargs="+",
        metavar="NAME TYPE",
        help="one or more 'name type' pairs, e.g. www.example. A",
    )
    return parser


def run(argv: Sequence[str], *, stdout: TextIO = sys.stdout) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv))
    if len(args.queries) % 2 != 0:
        parser.error("queries must come in 'name type' pairs")

    try:
        zone = load_zone(args.zone)
    except (OSError, ZoneError, RecordError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    pairs = list(zip(args.queries[0::2], args.queries[1::2]))
    rendered: List[str] = []
    for index, (name, rtype) in enumerate(pairs):
        try:
            response = zone.lookup(name, rtype)
        except (ZoneError, RecordError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        if index:
            rendered.append("")
        rendered.append(_format_response(response, as_json=args.json))

    stdout.write("\n".join(rendered) + "\n")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
