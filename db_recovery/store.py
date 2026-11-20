"""页存储抽象与两种本地实现。

* :class:`MemoryPageStore`：字典后端，供合成数据测试使用；
* :class:`DirectoryPageStore`：目录后端，每个页号一个 ``page-<十进制号>`` 文件，
  用于真实的"当前页文件 / 旧完整页备份"两套目录。

存储层只搬运字节，不解释页内容；页格式的解析与校验在 :mod:`db_recovery.page`
与 :mod:`db_recovery.recovery` 中完成。
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from typing import Iterable, Optional


class PageStore(ABC):
    """页号 -> 原始页字节的映射接口。"""

    @abstractmethod
    def read_page(self, page_id: int) -> Optional[bytes]:
        """读取一页；不存在返回 ``None``。"""

    @abstractmethod
    def write_page(self, page_id: int, blob: bytes) -> None:
        """写入（覆盖）一页。"""

    @abstractmethod
    def list_pages(self) -> Iterable[int]:
        """列出存储中存在的全部页号。"""


class MemoryPageStore(PageStore):
    """内存字典后端，主要用于本地合成数据测试。"""

    def __init__(self, pages: Optional[dict[int, bytes]] = None):
        self._pages: dict[int, bytes] = dict(pages or {})

    def read_page(self, page_id: int) -> Optional[bytes]:
        return self._pages.get(page_id)

    def write_page(self, page_id: int, blob: bytes) -> None:
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            raise TypeError("page blob must be bytes-like")
        self._pages[page_id] = bytes(blob)

    def list_pages(self) -> Iterable[int]:
        return list(self._pages.keys())

    def __contains__(self, page_id: int) -> bool:
        return page_id in self._pages


_PAGE_FILE_RE = re.compile(r"page-(\d+)$")


class DirectoryPageStore(PageStore):
    """目录后端：每页一个文件，文件名 ``page-<页号>``。

    目录中不符合命名规则的文件在列举时忽略，但不会被删除或改动。
    """

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)

    def _path(self, page_id: int) -> str:
        return os.path.join(self.directory, f"page-{page_id}")

    def read_page(self, page_id: int) -> Optional[bytes]:
        path = self._path(page_id)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def write_page(self, page_id: int, blob: bytes) -> None:
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            raise TypeError("page blob must be bytes-like")
        # 先写临时文件再替换，避免恢复发布过程中留下半页。
        tmp_path = self._path(page_id) + ".tmp"
        with open(tmp_path, "wb") as handle:
            handle.write(bytes(blob))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._path(page_id))

    def list_pages(self) -> Iterable[int]:
        ids: list[int] = []
        for name in os.listdir(self.directory):
            match = _PAGE_FILE_RE.match(name)
            if match:
                ids.append(int(match.group(1)))
        return sorted(ids)
