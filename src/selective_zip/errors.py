"""采集过程中可能抛出的错误类型。

所有错误都带有面向用户的中文说明，网页端可以直接展示。
"""

from __future__ import annotations


class SelectiveFetchError(Exception):
    """采集错误基类。"""

    #: 机器可读的错误代码，网页端可据此做分类展示。
    code = "fetch_error"


class UnsupportedZipSubset(SelectiveFetchError):
    """数据包不属于本项目明确声明支持的 ZIP/ZIP64 子集。"""

    code = "unsupported_zip_subset"


class EncryptedArchive(UnsupportedZipSubset):
    """数据包或选中成员使用了加密，本项目拒绝采集。"""

    code = "encrypted_archive"


class MultiVolumeArchive(UnsupportedZipSubset):
    """数据包是分卷（多卷） ZIP，本项目拒绝采集。"""

    code = "multi_volume_archive"


class UnsupportedCompression(UnsupportedZipSubset):
    """成员使用了未声明支持的压缩方式。"""

    code = "unsupported_compression"


class UnsupportedVersion(UnsupportedZipSubset):
    """成员声明的提取版本超出支持范围。"""

    code = "unsupported_version"


class RangeNotSupported(SelectiveFetchError):
    """源站不支持 Range 请求，无法做选择性采集。"""

    code = "range_not_supported"


class RangeRequestError(SelectiveFetchError):
    """Range 请求本身失败（非 206 响应、长度不符等）。"""

    code = "range_request_error"


class ZipStructureError(SelectiveFetchError):
    """ZIP 结构损坏或无法解析（找不到 EOCD、截断、签名错误等）。"""

    code = "zip_structure_error"


class HeaderMismatch(SelectiveFetchError):
    """中央目录条目与本地文件头不一致。"""

    code = "header_mismatch"


class CrcMismatch(SelectiveFetchError):
    """解压后数据的 CRC-32 与中央目录声明不符。"""

    code = "crc_mismatch"


class SizeMismatch(SelectiveFetchError):
    """解压后大小与声明不符。"""

    code = "size_mismatch"


class SourceChanged(SelectiveFetchError):
    """采集过程中源包发生变化（ETag/内容指纹变化）。"""

    code = "source_changed"


class MemberNotFound(SelectiveFetchError):
    """用户指定的成员在中央目录中不存在。"""

    code = "member_not_found"


class MemberRejected(SelectiveFetchError):
    """成员内容未通过记录级校验（跨版本 / 不完整）。"""

    code = "member_rejected"
