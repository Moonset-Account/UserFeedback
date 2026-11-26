"""Compatibility shim: ``import deid``."""

from deid_gateway import *  # noqa: F401,F403
from deid_gateway import (  # noqa: F401
    DeidGateway,
    Deidentifier,
    FieldDictionary,
    UidMapper,
    process_tree,
)
