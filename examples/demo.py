#!/usr/bin/env python3
"""Local demo: serve a synthetic list over HTTP and screen a few passwords.

Run from the repository root::

    python examples/demo.py

Everything is local and synthetic; no network access beyond 127.0.0.1.
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from password_screening import (  # noqa: E402
    HTTPTransport,
    ScreeningClient,
    ScreeningService,
    VersionStore,
    serve,
)
from password_screening.digest import password_digest  # noqa: E402

BLOCKLIST = ["hunter2", "swordfish", "letmein", "correct horse battery staple"]


def main() -> int:
    store = VersionStore(prefix_length=5, page_size=8, retain=4)
    version = store.publish([password_digest(p) for p in BLOCKLIST], version_id="demo-1")

    server = serve(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    transport = HTTPTransport(f"http://{host}:{port}")
    client = ScreeningClient(transport, prefix_length=store.prefix_length)

    print(f"version={version.version_id} pages={version.total_pages} page_size={store.page_size}")
    try:
        for password in ["hunter2", "swordfish", "not-on-the-list", "zzz"]:
            result = client.screen(password)
            verdict = "BLOCKED" if result.matched else "allowed"
            print(f"{password!r:38} -> {verdict} (pages={result.pages_read})")

        # Publish a replacement mid-traversal.  The in-flight verdict still
        # belongs to demo-1 alone; only a fresh traversal sees demo-2.
        def update_on_first_page(page_index, response):
            if page_index == 0 and store.current_version_id == "demo-1":
                store.publish(
                    [password_digest(p) for p in BLOCKLIST + ["newly-added"]],
                    version_id="demo-2",
                )

        late = client.screen_digest(password_digest("newly-added"), on_page=update_on_first_page)
        print(
            f"in-flight traversal of demo-1: matched={late.matched} "
            f"version={late.version_id} (current is now {store.current_version_id})"
        )
        print("fresh traversal:", client.screen("newly-added"))

        service = ScreeningService(store)
        print("info:", service.handle({"op": "info"}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
