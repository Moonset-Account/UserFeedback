"""ZIP / ZIP64 二进制结构常量与协议子集声明。

参考：PKWARE APPNOTE.TXT 6.3.x（ZIP Application Note）。
本项目只实现选择性远程采集所需的解析逻辑，不是通用解压库。

明确声明支持的协议子集
----------------------
- 容器：单卷（single-disk）、未加密的 ZIP 与 ZIP64；
- 压缩方式：仅 ``stored``（0，不压缩）与 ``deflate``（8，见 RFC 1951）；
- 提取所需版本：<= 45（4.5，ZIP64）；
- 文件名编码：传统 CP437 与 UTF-8（通用位标志 bit 11）；
- 数据描述符：支持通用位标志 bit 3 的流式条目（含/不含签名均可）；
- 目录定位：EOCD、ZIP64 EOCD Locator、ZIP64 EOCD Record；
- 完整性：CRC-32、压缩/未压缩大小、本地头与中央目录一致性校验。

明确拒绝
--------
- 任何形式的加密（传统 PKWARE 加密、强加密、AE 等，通用位标志 bit 0）；
- 分卷/多卷归档（EOCD 盘号字段、通用位标志 bit 3 的分卷语义）；
- 压缩方式不在 {0, 8} 内、提取版本 > 45 的成员。
"""

from __future__ import annotations

# ---- 签名（小端四字节）------------------------------------------------
SIG_LOCAL_FILE_HEADER = b"PK\x03\x04"
SIG_CENTRAL_DIR_HEADER = b"PK\x01\x02"
SIG_END_OF_CENTRAL_DIR = b"PK\x05\x06"
SIG_ZIP64_EOCD = b"PK\x06\x06"
SIG_ZIP64_EOCD_LOCATOR = b"PK\x06\x07"
SIG_DATA_DESCRIPTOR = b"PK\x07\x08"

# ---- 固定结构字节数 ----------------------------------------------------
SIZE_LOCAL_FILE_HEADER_FIXED = 30
SIZE_CENTRAL_DIR_HEADER_FIXED = 46
SIZE_EOCD_FIXED = 22
SIZE_ZIP64_EOCD_LOCATOR = 20
SIZE_ZIP64_EOCD_RECORD_FIXED = 56
ZIP64_EOCD_LOCATOR_OFFSET_FROM_EOCD = 20
MAX_EOCD_COMMENT = 65535
# EOCD 搜索窗口：22 字节固定头 + 最大注释
EOCD_SEARCH_WINDOW = SIZE_EOCD_FIXED + MAX_EOCD_COMMENT

# ---- 压缩方式 ----------------------------------------------------------
COMPRESSION_STORED = 0
COMPRESSION_DEFLATE = 8
SUPPORTED_COMPRESSIONS = frozenset({COMPRESSION_STORED, COMPRESSION_DEFLATE})

# ---- 版本 --------------------------------------------------------------
# 高位 * 10 表示主版本，低位为次版本；45 == 4.5（ZIP64）
MAX_EXTRACT_VERSION = 45
ZIP64_VERSION = 45

# ---- 通用位标志 --------------------------------------------------------
FLAG_ENCRYPTED = 1 << 0
FLAG_DATA_DESCRIPTOR = 1 << 3
FLAG_UTF8_NAMES = 1 << 11

# ---- 特殊长度占位（ZIP64）--------------------------------------------
ZIP64_MAGIC = 0xFFFFFFFF
UINT16_MAX = 0xFFFF
UINT32_MAX = 0xFFFFFFFF

# ---- 本地头读取块大小（首次试探读取，覆盖绝大多数头）----------------
LOCAL_HEADER_PROBE = 4096

# ---- 人类可读的子集声明，供网页与 README 展示 ------------------------
SUPPORTED_SUBSET_DESCRIPTION = (
    "单卷、未加密 ZIP/ZIP64；stored/deflate；提取版本<=4.5；"
    "支持 UTF-8/CP437 文件名与数据描述符；拒绝加密、分卷及其他压缩方式。"
)
