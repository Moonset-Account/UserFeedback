"""端到端冒烟演示：逐条展示题面准入规则的实际判定。

运行：python3 -I demo_smoke.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from admission import (
    admit,
    CreateFile,
    EstablishDirectory,
    InheritDirectory,
    LockDirectory,
    MoveObject,
    Sequence,
    UnknownBranch,
)


def show(title, program):
    report = admit(program)
    print(f"== {title}: {report.verdict.value}")
    for entry in report.entries:
        loc = "/".join(map(str, entry.branch_path)) or "-"
        print(
            f"  #{entry.sequence_index} [{loc}] {entry.verdict.value:6} "
            f"{entry.reason:45} dir={entry.basis_directory_id} "
            f"id={entry.basis_identity} obj={entry.object_id}"
        )
    print()


show("正常流程", Sequence([
    EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
    LockDirectory(directory_id="d1", owner="alice"),
    CreateFile(id="f1", name="a.tmp", directory_id="d1", creator="alice"),
    MoveObject(object_id="f1", target_directory_id="d1", target_name="b.tmp"),
]))

show("未锁定接收对象", Sequence([
    EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
    CreateFile(id="f1", name="a.tmp", directory_id="d1", creator="alice"),
]))

show("移动源非私有", Sequence([
    EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
    LockDirectory(directory_id="d1", owner="alice"),
    CreateFile(id="f1", name="a.tmp", directory_id="d1",
               creator="alice", private=False),
    MoveObject(object_id="f1", target_directory_id="d1",
               target_name="c.tmp"),
]))

show("分支未知所有权", Sequence([
    EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
    LockDirectory(directory_id="d1", owner="alice"),
    UnknownBranch(affected_directory_ids=["d1"]),
    CreateFile(id="f1", name="a.tmp", directory_id="d1", creator="alice"),
]))

show("继承目录未确认即锁定(身份不符)", Sequence([
    InheritDirectory(id="d9", path="/srv/s", expected_owner="alice"),
    LockDirectory(directory_id="d9", owner="mallory"),
]))

show("路径名复用不继承授权", Sequence([
    EstablishDirectory(id="d1", path="/tmp/a", owner="alice"),
    LockDirectory(directory_id="d1", owner="alice"),
    CreateFile(id="f1", name="a.tmp", directory_id="d1", creator="alice"),
    MoveObject(object_id="f1", target_directory_id="d1",
               target_name="x.tmp"),
    CreateFile(id="f3", name="a.tmp", directory_id="d1", creator="bob"),
]))
