"""本地页面存储的崩溃恢复基础设施。

模块组成：

* :mod:`db_recovery.page`     固定页格式（提交号 + CRC32 校验）
* :mod:`db_recovery.journal`  追加日志帧格式、写入器与读取器
* :mod:`db_recovery.store`    页存储抽象（内存实现 / 目录实现）
* :mod:`db_recovery.recovery` 恢复主流程
"""

from .journal import (
    COMMIT,
    WRITE,
    CommitRecord,
    JournalCorrupt,
    JournalWriter,
    TruncatedJournal,
    WriteRecord,
    read_journal,
)
from .page import PAGE_HEADER_SIZE, Page, build_page, parse_page, page_crc
from .recovery import RecoveryError, RecoveryResult, recover
from .store import DirectoryPageStore, MemoryPageStore, PageStore

__all__ = [
    "COMMIT",
    "WRITE",
    "CommitRecord",
    "JournalCorrupt",
    "JournalWriter",
    "TruncatedJournal",
    "WriteRecord",
    "read_journal",
    "PAGE_HEADER_SIZE",
    "Page",
    "build_page",
    "parse_page",
    "page_crc",
    "RecoveryError",
    "RecoveryResult",
    "recover",
    "DirectoryPageStore",
    "MemoryPageStore",
    "PageStore",
]
