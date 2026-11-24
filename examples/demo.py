"""Small walk-through of the guard.  Run: python3 examples/demo.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backup_guard import (  # noqa: E402
    BackupReadGuard,
    FileCategory,
    LockEvent,
    PowerEvent,
)


def main() -> None:
    g = BackupReadGuard()
    g.feed(PowerEvent.boot("boot-1", 0))
    print("after boot:", g.state.describe())

    g.start_read("dev-1", FileCategory.DEVICE_ENCRYPTED)
    g.feed(LockEvent.unlock("boot-1", 1))
    g.start_read("cred-1", FileCategory.CREDENTIAL_ENCRYPTED)
    g.start_read("strict-1", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)

    g.feed(LockEvent.lock("boot-1", 2))
    print("after mid-read lock, open:", [r.shard_id for r in g.open_reads])
    print("strict-1 status:", g.records("strict-1")[0].status.value)
    g.finish_read("cred-1")
    g.finish_read("dev-1")

    g.feed(LockEvent.unlock("boot-1", 3))
    g.start_read("strict-1", FileCategory.CREDENTIAL_ENCRYPTED_STRICT)
    g.finish_read("strict-1")

    print("pack check:")
    for line in g.pack_check().explain():
        print("  " + line)

    g.feed(PowerEvent.shutdown("boot-1", 4))
    g.feed(PowerEvent.boot("boot-2", 5))
    print("after reboot, first_unlock_done =", g.state.first_unlock_done)
    print("pack check:")
    for line in g.pack_check().explain():
        print("  " + line)


if __name__ == "__main__":
    main()
