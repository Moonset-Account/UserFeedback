"""Message compression library for bounded integer register updates.

Public API
----------

Declarations and messages:
    Declaration, Operation, Stream, Update, Barrier, Receipt,
    InvalidDeclaration, InvalidCandidate

Semantics:
    compare_streams, is_equivalent, CandidateVerdict, Mismatch,
    symbolic_trace, concrete_replay, execute_concrete

Compression:
    compress_stream, compress_segment, split_segments, CompressionResult

Encoding:
    encode_stream, decode_stream, encoded_length,
    encode_declaration, decode_declaration
"""

from .compression import (
    CompressionResult,
    Segment,
    compress_segment,
    compress_stream,
    split_segments,
)
from .encoding import (
    decode_declaration,
    decode_stream,
    encode_declaration,
    encode_stream,
    encoded_length,
    message_body_length,
)
from .model import (
    ASSIGN,
    COND,
    SADD,
    SWAP,
    Barrier,
    Declaration,
    InvalidCandidate,
    InvalidDeclaration,
    Message,
    Operation,
    Receipt,
    Stream,
    Update,
)
from .semantics import (
    CandidateVerdict,
    Mismatch,
    SymbolicTrace,
    TraceEntry,
    compare_streams,
    concrete_replay,
    is_equivalent,
    symbolic_trace,
)
from .symbolic import State, execute_concrete, initial_state

__all__ = [
    "ASSIGN",
    "COND",
    "SADD",
    "SWAP",
    "Barrier",
    "CandidateVerdict",
    "CompressionResult",
    "Declaration",
    "InvalidCandidate",
    "InvalidDeclaration",
    "Message",
    "Mismatch",
    "Operation",
    "Receipt",
    "Segment",
    "State",
    "Stream",
    "SymbolicTrace",
    "TraceEntry",
    "Update",
    "compare_streams",
    "compress_segment",
    "compress_stream",
    "concrete_replay",
    "decode_declaration",
    "decode_stream",
    "encode_declaration",
    "encode_stream",
    "encoded_length",
    "execute_concrete",
    "initial_state",
    "is_equivalent",
    "message_body_length",
    "split_segments",
    "symbolic_trace",
]
