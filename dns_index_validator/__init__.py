"""DNS label-index integrity validator.

Downstream name-expansion modules must only consume resource records whose
referenced names passed the integrity checks implemented in this package.
"""

from .model import (
    IntegrityError,
    NameReport,
    NodeRecord,
    ResourceRecord,
)
from .validator import validate_name_index

__all__ = [
    "IntegrityError",
    "NameReport",
    "NodeRecord",
    "ResourceRecord",
    "validate_name_index",
]
