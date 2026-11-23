"""快照持久化：把协调器的完整状态落到本地 JSON，重启后恢复。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .engine import RemediationCoordinator
from .executor import Executor


def save_snapshot(coordinator: RemediationCoordinator, path: str | os.PathLike[str]) -> None:
    """原子写入快照：先写临时文件再替换，避免重启读到半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(coordinator.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load_snapshot(
    path: str | os.PathLike[str], executor: Executor | None = None
) -> RemediationCoordinator:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return RemediationCoordinator.restore(data, executor=executor)
