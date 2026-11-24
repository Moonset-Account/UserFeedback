"""序列目录：管理多个序列、会话与 JSON 持久化。"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from .exceptions import (
    SequenceConfigError,
    SequenceNotFoundError,
)
from .model import MIN_BIGINT, MAX_BIGINT, SequenceConfig, SequenceSession, SequenceState, _SequenceEntry

#: 持久化文件的格式版本，便于以后演进。
PERSIST_FORMAT_VERSION = 1


class SequenceCatalog:
    """序列目录，一个目录管理多个命名序列。

    目录自身的锁保护序列表；单个序列的预留/发放由条目内部的锁保护。
    支持通过 :meth:`save` / :meth:`load` 保存和恢复预留状态（watermark），
    进程退出后遗留在会话里的号码不会被恢复，因而允许空缺。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _SequenceEntry] = {}
        # 持有打开的会话，管理变更时主动令其失效。
        self._sessions: list[SequenceSession] = []

    # ---- 序列管理 ------------------------------------------------------

    def create_sequence(
        self,
        name: str,
        start_value: int = 1,
        increment_by: int = 1,
        min_value: int = MIN_BIGINT,
        max_value: int = MAX_BIGINT,
        cache_size: int = 20,
        cycle: bool = False,
    ) -> SequenceConfig:
        """创建并登记一个序列，返回其配置。"""
        config = SequenceConfig(
            name=name,
            start_value=start_value,
            increment_by=increment_by,
            min_value=min_value,
            max_value=max_value,
            cache_size=cache_size,
            cycle=cycle,
        )
        with self._lock:
            if name in self._entries:
                raise SequenceConfigError(f"序列 {name!r} 已存在")
            self._entries[name] = _SequenceEntry(config)
        return config

    def drop_sequence(self, name: str) -> None:
        """删除序列；仍打开的会话会被标记关闭，再取号将报错。"""
        with self._lock:
            entry = self._require(name)
            for session in self._sessions:
                if session._entry is entry:  # noqa: SLF001 - 同包协作
                    session.close()
            del self._entries[name]
            self._sessions = [s for s in self._sessions if not s.closed]

    def get_config(self, name: str) -> SequenceConfig:
        with self._lock:
            return self._require(name).config

    def get_state(self, name: str) -> SequenceState:
        """返回状态快照（副本），便于观测与断言。"""
        with self._lock:
            state = self._require(name).state
            return SequenceState(
                watermark=state.watermark,
                version=state.version,
                reclaimed_batches=state.reclaimed_batches,
            )

    def list_sequences(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    # ---- 会话 ----------------------------------------------------------

    def open_session(self, name: str) -> SequenceSession:
        """打开一个取号会话。"""
        with self._lock:
            entry = self._require(name)
            session = SequenceSession(entry)
            self._sessions.append(session)
            return session

    # ---- 管理变更 ------------------------------------------------------

    def alter_increment(self, name: str, increment_by: int) -> None:
        """改步长。

        收回所有会话尚未消费的号段（会话下一次 ``nextval`` 时按新版本重新
        预留），watermark 保持在旧版本已预留到的边界，沿新步长继续发放。
        """
        with self._lock:
            entry = self._require(name)
            with entry.lock:
                entry.alter_increment(increment_by)
            self._invalidate_sessions(entry)

    def restart_with(self, name: str, next_value: int) -> None:
        """显式指定下一值，同样收回所有尚未消费的会话号段。"""
        with self._lock:
            entry = self._require(name)
            with entry.lock:
                entry.restart_with(next_value)
            self._invalidate_sessions(entry)

    def _invalidate_sessions(self, entry: _SequenceEntry) -> None:
        """让持有该序列旧号段的会话缓存失效（会话自身也有惰性兜底）。"""
        for session in self._sessions:
            if session._entry is entry:  # noqa: SLF001
                with entry.lock:
                    if not session.closed:
                        session._cache.clear()  # noqa: SLF001
                        session._token = None  # noqa: SLF001
                        session._version = entry.state.version  # noqa: SLF001
        self._sessions = [s for s in self._sessions if not s.closed]

    # ---- 持久化 --------------------------------------------------------

    def save(self, path: str) -> None:
        """把所有序列的配置与预留状态保存为 JSON 文件（原子替换）。"""
        with self._lock:
            payload = {
                "format_version": PERSIST_FORMAT_VERSION,
                "sequences": {
                    name: {
                        "config": {
                            "name": entry.config.name,
                            "start_value": entry.config.start_value,
                            "increment_by": entry.config.increment_by,
                            "min_value": entry.config.min_value,
                            "max_value": entry.config.max_value,
                            "cache_size": entry.config.cache_size,
                            "cycle": entry.config.cycle,
                        },
                        "state": entry.state.to_dict(),
                    }
                    for name, entry in self._entries.items()
                },
            }
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".seqcatalog-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str) -> "SequenceCatalog":
        """从 :meth:`save` 写出的文件恢复目录。

        只恢复 watermark（已预留边界）与版本信息；会话级缓存无法跨进程
        恢复，故旧进程未消费的号码成为空缺。
        """
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or payload.get("format_version") != PERSIST_FORMAT_VERSION:
            raise SequenceConfigError("状态文件缺失或 format_version 不支持")
        raw_sequences = payload.get("sequences")
        if not isinstance(raw_sequences, dict):
            raise SequenceConfigError("状态文件中的 sequences 必须是对象")

        catalog = cls()
        for name, raw in raw_sequences.items():
            if not isinstance(raw, dict):
                raise SequenceConfigError(f"序列 {name!r} 的持久化记录格式非法")
            config = SequenceConfig(**raw["config"])
            if config.name != name:
                raise SequenceConfigError(f"序列 {name!r} 的名称与配置不一致")
            state = SequenceState.from_dict(raw["state"])
            with catalog._lock:
                catalog._entries[name] = _SequenceEntry(config, state)
        return catalog

    # ---- 内部 ----------------------------------------------------------

    def _require(self, name: str) -> _SequenceEntry:
        try:
            return self._entries[name]
        except KeyError:
            raise SequenceNotFoundError(f"序列 {name!r} 不存在") from None
