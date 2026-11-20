"""模式注册中心。

流程：
1. register_writer 登记写入模式，分配版本号，默认进入保留期。
2. register_reader 登记读取模式前，按版本顺序逐一比较仍在保留期的
   写入模式；遇到第一个不兼容的旧版本即抛出 CompatibilityError，
   其中携带第一个不兼容路径与该旧版本号。全部兼容才完成登记。
3. 只有登记成功的读取模式才允许通过 read 按同一套规则读取旧消息。
4. retire_writer 将写出版本移出保留期，之后不再参与比较。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .compat import Issue, check_record_compatible, validate_declared_defaults
from .reader import read_message
from .types import RecordType


class RegistryError(Exception):
    """注册中心操作错误（重复版本、未登记读取模式等）。"""


class CompatibilityError(Exception):
    """读取模式与某个保留期写入模式不兼容。

    old_version: 第一个不兼容的旧版本号。
    issues: 该版本下的全部不兼容问题，首项为第一个不兼容路径。
    """

    def __init__(self, old_version: int, issues: List[Issue]):
        if not issues:
            raise ValueError("issues 不能为空")
        self.old_version = old_version
        self.issues = list(issues)
        first = self.issues[0]
        super().__init__(
            "与保留期写入模式 v%d 不兼容，第一个不兼容路径 %s: %s"
            % (old_version, first.path or "<root>", first.reason)
        )


@dataclass
class _Writer:
    schema: RecordType
    retained: bool = True


class SchemaRegistry:
    def __init__(self) -> None:
        self._writers: Dict[int, _Writer] = {}
        self._readers: Dict[str, RecordType] = {}
        self._next_version = 1

    # ---- 写入模式 ----

    def register_writer(self, schema: RecordType, *, version: Optional[int] = None) -> int:
        if not isinstance(schema, RecordType):
            raise RegistryError("写入模式必须是具名记录")
        bad = validate_declared_defaults(schema)
        if bad:
            raise RegistryError("写入模式缺省值不合法: %s" % bad[0])
        if version is None:
            version = self._next_version
        elif version in self._writers:
            raise RegistryError("写入版本 v%d 已存在" % version)
        self._writers[version] = _Writer(schema)
        self._next_version = max(self._next_version, version + 1)
        return version

    def retire_writer(self, version: int) -> None:
        """将写出版本移出保留期，不再参与读取模式登记时的比较。"""
        try:
            self._writers[version].retained = False
        except KeyError:
            raise RegistryError("未知写入版本 v%d" % version) from None

    def retained_versions(self) -> List[int]:
        return sorted(v for v, w in self._writers.items() if w.retained)

    # ---- 读取模式 ----

    def register_reader(self, name: str, schema: RecordType) -> None:
        if not isinstance(schema, RecordType):
            raise RegistryError("读取模式必须是具名记录")
        bad = validate_declared_defaults(schema)
        if bad:
            raise RegistryError("读取模式 %s 缺省值不合法: %s" % (name, bad[0]))
        # 逐一比较仍在保留期的写入模式（按版本顺序）
        for version in sorted(self._writers):
            writer = self._writers[version]
            if not writer.retained:
                continue
            issues = check_record_compatible(schema, writer.schema)
            if issues:
                raise CompatibilityError(version, issues)
        # 全部兼容后才允许登记
        self._readers[name] = schema

    def is_reader_registered(self, name: str) -> bool:
        return name in self._readers

    # ---- 读取旧消息 ----

    def read(self, reader_name: str, writer_version: int, message: Any) -> dict:
        """只有登记成功的读取模式才允许按同一规则读取旧消息。"""
        schema = self._readers.get(reader_name)
        if schema is None:
            raise RegistryError(
                "读取模式 %s 未登记，禁止读取旧消息" % reader_name
            )
        try:
            writer = self._writers[writer_version]
        except KeyError:
            raise RegistryError("未知写入版本 v%d" % writer_version) from None
        return read_message(schema, writer.schema, message)
