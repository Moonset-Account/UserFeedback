"""Compatibility shim: ``import gateway``.

The implementation lives in the :mod:`deid_gateway` package; this module
re-exports its public API for callers that expect a top level
``gateway`` module (the prose calls this component the de-identification
gateway processing module).
"""

from deid_gateway import *  # noqa: F401,F403
from deid_gateway.gateway import (  # noqa: F401
    DeidGateway,
    DeidentificationGateway,
    DicomDeidentifier,
    ExportResult,
    Gateway,
)
from deid_gateway.processor import process_attribute_tree, process_tree  # noqa: F401
from deid_gateway.uids import UidMapper  # noqa: F401

if __name__ == "__main__":  # pragma: no cover
    from deid_gateway.cli import main

    raise SystemExit(main())
