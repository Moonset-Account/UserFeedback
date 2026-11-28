"""端到端冒烟演示（合成数据，无网络、无外部依赖）。

运行：python3 -I smoke_demo.py
"""

import os
import sys

# -I 隔离模式不自动把脚本目录加入 sys.path，这里显式加入工作区根目录。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone

from key_coverage import (
    DirectoryStore,
    MemberKind,
    Status,
    check_key_coverage,
)
from key_coverage.model import (
    DirectorySnapshot,
    Group,
    KeyRecord,
    MemberRef,
    Person,
)

UTC = timezone.utc
WHEN = datetime(2026, 11, 28, 12, tzinfo=UTC)


def main() -> None:
    # 自引用组 g-loop：真实成员 alice + 自环；root 再用 secret 边引用 alice。
    loop = Group(
        id="g-loop",
        name="值班循环组",
        members=(
            MemberRef("p-alice", MemberKind.PERSON),
            MemberRef("p-bob", MemberKind.PERSON),
            MemberRef("g-loop", MemberKind.GROUP),  # 自引用
        ),
    )
    root = Group(
        id="g-root",
        name="全局通知组",
        members=(
            MemberRef("g-loop", MemberKind.GROUP),
            MemberRef("p-alice", MemberKind.PERSON, min_level="secret"),
        ),
    )
    persons = (
        Person(id="p-alice", name="Alice", keys=(KeyRecord("k-alice"),)),
        # bob 的密钥昨天过期。
        Person(
            id="p-bob",
            name="Bob",
            keys=(
                KeyRecord(
                    "k-bob-old",
                    valid_to=datetime(2026, 11, 27, tzinfo=UTC),
                ),
            ),
        ),
    )
    snapshot = DirectorySnapshot(
        version="v2026-11-28",
        groups={g.id: g for g in (root, loop)},
        persons={p.id: p for p in persons},
    )
    result = check_key_coverage(
        DirectoryStore({snapshot.version: snapshot}),
        directory_version="v2026-11-28",
        root_group_id="g-root",
        notification_level="internal",
        scheduled_at=WHEN,
    )

    print("状态:", result.status.value)
    print("环截断次数:", len(result.cycle_cuts))
    print("到达人员:", result.reached_person_ids)
    print("最终收件人(按密钥去重):", [(r.person_name, r.key_id) for r in result.recipients])
    assert result.status is Status.PREPARING
    failure = result.missing_key_failures[0]
    print("缺密钥人员:", failure.person_name)
    print("原因链:", failure.reason_chain)


if __name__ == "__main__":
    main()
