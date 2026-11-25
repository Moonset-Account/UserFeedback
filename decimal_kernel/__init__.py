"""Integer-only batch conversion from signed packed BCD DECIMAL to fixed int."""

from .api import (
    BatchResult,
    ContiguousPackedColumn,
    ErrorMode,
    PackedDecimalBatchConverter,
    convert_packed_batch,
    convert_packed_page,
)
from .bitmap import BitOrder, NullBitmap
from .codec import decode_packed_mantissa
from .errors import (
    BatchConversionError,
    BitmapError,
    ConfigurationError,
    ConversionKernelError,
    ErrorCode,
    RowConversionException,
    RowError,
    row_error_to_dict,
)
from .rounding import rescale_to_target_scale
from .spec import SourceColumn, TargetColumn

__all__ = [
    "BatchConversionError",
    "BatchResult",
    "BitmapError",
    "BitOrder",
    "ConfigurationError",
    "ContiguousPackedColumn",
    "ConversionKernelError",
    "ErrorCode",
    "ErrorMode",
    "NullBitmap",
    "PackedDecimalBatchConverter",
    "RowConversionException",
    "RowError",
    "SourceColumn",
    "TargetColumn",
    "convert_packed_batch",
    "convert_packed_page",
    "decode_packed_mantissa",
    "rescale_to_target_scale",
    "row_error_to_dict",
]
