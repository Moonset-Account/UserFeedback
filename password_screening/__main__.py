#!/usr/bin/env python3
"""Command-line entry point for local screening.

Examples::

    # Build a version from a synthetic digest list and screen passwords locally.
    python -m password_screening screen --digests-file examples/digests.txt \
        --password hunter2 --password nope

    # Serve a version over loopback HTTP (press Ctrl-C to stop).
    python -m password_screening serve --digests-file examples/digests.txt --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Iterable

from .client import HTTPTransport, LocalTransport, ScreeningClient
from .digest import normalize_digest_list
from .server import ScreeningService, serve
from .store import VersionStore


def _read_digests(paths: Iterable[str]) -> list[str]:
    collected: list[str] = []
    for raw in paths:
        path = Path(raw)
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            collected.append(line)
    return list(normalize_digest_list(collected))


def _build_store(args: argparse.Namespace) -> VersionStore:
    store = VersionStore(
        prefix_length=args.prefix_length,
        page_size=args.page_size,
        retain=args.retain,
    )
    digests = _read_digests(args.digests_file) if args.digests_file else []
    store.publish(digests, version_id=args.version)
    return store


def _cmd_screen(args: argparse.Namespace) -> int:
    store = _build_store(args)
    client = ScreeningClient(
        LocalTransport(ScreeningService(store)),
        prefix_length=store.prefix_length,
        expected_page_size=store.page_size,
    )
    blocked = 0
    for password in args.password:
        result = client.screen(password)
        blocked += int(result.matched)
        record = {
            "password": password,
            "matched": result.matched,
            "version": result.version_id,
            "pages_read": result.pages_read,
        }
        print(json.dumps(record, sort_keys=True))
    return 1 if blocked and args.fail_on_match else 0


def _cmd_serve(args: argparse.Namespace) -> int:
    store = _build_store(args)
    version = store.current
    server = serve(store, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(
        f"serving version {version.version_id} "
        f"(prefix={store.prefix_length}, page_size={store.page_size}, "
        f"pages={version.total_pages}) on http://{host}:{port}/lookup",
        flush=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    client = ScreeningClient(
        HTTPTransport(args.url),
        prefix_length=args.prefix_length,
        expected_page_size=args.page_size,
    )
    result = client.screen(args.password)
    print(
        json.dumps(
            {
                "matched": result.matched,
                "version": result.version_id,
                "pages_read": result.pages_read,
                "restarts": result.restarts,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="password_screening", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--digests-file",
        action="append",
        default=[],
        metavar="PATH",
        help="file of 64-hex digests (repeatable, # comments allowed)",
    )
    common.add_argument("--prefix-length", type=int, default=5)
    common.add_argument("--page-size", type=int, default=8)
    common.add_argument("--retain", type=int, default=4)
    common.add_argument("--version", default=None)

    screen_cmd = sub.add_parser("screen", parents=[common], help="screen passwords locally")
    screen_cmd.add_argument("--password", action="append", required=True)
    screen_cmd.add_argument("--fail-on-match", action="store_true")
    screen_cmd.set_defaults(func=_cmd_screen)

    serve_cmd = sub.add_parser("serve", parents=[common], help="serve a version over HTTP")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.set_defaults(func=_cmd_serve)

    probe_cmd = sub.add_parser("probe", help="query a running service")
    probe_cmd.add_argument("--url", required=True)
    probe_cmd.add_argument("--password", required=True)
    probe_cmd.add_argument("--prefix-length", type=int, default=5)
    probe_cmd.add_argument("--page-size", type=int, default=8)
    probe_cmd.set_defaults(func=_cmd_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
