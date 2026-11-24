"""Byte encoding for declarations and message streams (LEB128, stdlib only).

Wire layout::

    magic  b"RMC1"
    u8     number of registers N
    N * (signed LEB128 lo, signed LEB128 hi)
    uLEB   number of constants
    constants: signed LEB128 each
    uLEB   number of operations
    operations: see _encode_operation
    uLEB   number of messages
    messages:
        tag 0: update          uLEB operation
        tag 1: update+receipt  uLEB operation, uLEB receipt id
        tag 2: barrier         uLEB count, count * uLEB register
        tag 3: barrier+receipt uLEB count, count * uLEB register, uLEB receipt
        tag 4: receipt         uLEB receipt id

All integer values use LEB128 (unsigned for indices/counts, signed for
declared constants and register bounds).
"""

from __future__ import annotations

from typing import List, Tuple

from .model import (
    ASSIGN,
    COND,
    SADD,
    SWAP,
    Barrier,
    Declaration,
    InvalidCandidate,
    Message,
    Operation,
    Receipt,
    Stream,
    Update,
)

MAGIC = b"RMC1"
_TAG_UPDATE = 0
_TAG_UPDATE_RECEIPT = 1
_TAG_BARRIER = 2
_TAG_BARRIER_RECEIPT = 3
_TAG_RECEIPT = 4

_KIND_IDS = {ASSIGN: 0, SWAP: 1, SADD: 2, COND: 3}
_ID_KINDS = {value: key for key, value in _KIND_IDS.items()}
_ACTION_IDS = {ASSIGN: 0, SADD: 1}
_ID_ACTIONS = {value: key for key, value in _ACTION_IDS.items()}


def encode_unsigned(value: int) -> bytes:
    if value < 0:
        raise ValueError("unsigned LEB128 requires a non-negative value")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_unsigned(data: bytes, offset: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated unsigned LEB128")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ValueError("unsigned LEB128 too long")


def encode_signed(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        next_value = value >> 7
        sign_bit = byte & 0x40
        if (next_value == 0 and not sign_bit) or (
            next_value == -1 and sign_bit
        ):
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)
        value = next_value


def decode_signed(data: bytes, offset: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated signed LEB128")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte & 0x40:
                result |= -(1 << shift)
            return result, offset
        if shift > 63:
            raise ValueError("signed LEB128 too long")


def _encode_operation(op: Operation) -> bytes:
    kind_id = _KIND_IDS[op.kind]
    out = bytearray([kind_id])
    out += encode_unsigned(op.target)
    if op.kind == SWAP:
        out += encode_unsigned(op.source)
    elif op.kind in (ASSIGN, SADD):
        out += encode_signed(op.constant)
    else:  # COND
        out += encode_unsigned(op.source)
        out += encode_signed(op.low)
        out += encode_signed(op.high)
        out += encode_signed(op.constant)
        out += encode_unsigned(_ACTION_IDS[op.action])
    return bytes(out)


def _decode_operation(data: bytes, offset: int) -> Tuple[Operation, int]:
    kind_id, offset = decode_unsigned(data, offset)
    if kind_id not in _ID_KINDS:
        raise ValueError("unknown operation kind id")
    kind = _ID_KINDS[kind_id]
    target, offset = decode_unsigned(data, offset)
    if kind == SWAP:
        source, offset = decode_unsigned(data, offset)
        return Operation(SWAP, target=target, source=source), offset
    if kind in (ASSIGN, SADD):
        constant, offset = decode_signed(data, offset)
        return Operation(kind, target=target, constant=constant), offset
    source, offset = decode_unsigned(data, offset)
    low, offset = decode_signed(data, offset)
    high, offset = decode_signed(data, offset)
    constant, offset = decode_signed(data, offset)
    action_id, offset = decode_unsigned(data, offset)
    if action_id not in _ID_ACTIONS:
        raise ValueError("unknown conditional action id")
    return (
        Operation(
            COND,
            target=target,
            source=source,
            constant=constant,
            low=low,
            high=high,
            action=_ID_ACTIONS[action_id],
        ),
        offset,
    )


def encode_declaration(declaration: Declaration) -> bytes:
    out = bytearray(MAGIC)
    out.append(len(declaration.registers))
    for lo, hi in declaration.registers:
        out += encode_signed(lo)
        out += encode_signed(hi)
    out += encode_unsigned(len(declaration.constants))
    for constant in declaration.constants:
        out += encode_signed(constant)
    out += encode_unsigned(len(declaration.operations))
    for operation in declaration.operations:
        out += _encode_operation(operation)
    return bytes(out)


def decode_declaration(data: bytes, offset: int = 0) -> Tuple[Declaration, int]:
    if data[offset:offset + 4] != MAGIC:
        raise ValueError("bad magic")
    offset += 4
    nregs, offset = decode_unsigned(data, offset)
    registers: List[Tuple[int, int]] = []
    for _ in range(nregs):
        lo, offset = decode_signed(data, offset)
        hi, offset = decode_signed(data, offset)
        registers.append((lo, hi))
    nconst, offset = decode_unsigned(data, offset)
    constants: List[int] = []
    for _ in range(nconst):
        value, offset = decode_signed(data, offset)
        constants.append(value)
    nops, offset = decode_unsigned(data, offset)
    operations: List[Operation] = []
    for _ in range(nops):
        operation, offset = _decode_operation(data, offset)
        operations.append(operation)
    declaration = Declaration(
        registers=tuple(registers),
        constants=tuple(constants),
        operations=tuple(operations),
    )
    return declaration, offset


def _encode_message(message: Message) -> bytes:
    if isinstance(message, Update):
        if message.receipt is None:
            return bytes([_TAG_UPDATE]) + encode_unsigned(message.operation)
        return (
            bytes([_TAG_UPDATE_RECEIPT])
            + encode_unsigned(message.operation)
            + encode_unsigned(message.receipt)
        )
    if isinstance(message, Barrier):
        body = encode_unsigned(len(message.registers))
        for register in message.registers:
            body += encode_unsigned(register)
        if message.receipt is None:
            return bytes([_TAG_BARRIER]) + body
        return bytes([_TAG_BARRIER_RECEIPT]) + body + encode_unsigned(message.receipt)
    if isinstance(message, Receipt):
        return bytes([_TAG_RECEIPT]) + encode_unsigned(message.receipt_id)
    raise InvalidCandidate(f"unknown message type {type(message).__name__}")


def _decode_message(data: bytes, offset: int) -> Tuple[Message, int]:
    tag = data[offset]
    offset += 1
    if tag == _TAG_UPDATE:
        operation, offset = decode_unsigned(data, offset)
        return Update(operation), offset
    if tag == _TAG_UPDATE_RECEIPT:
        operation, offset = decode_unsigned(data, offset)
        receipt, offset = decode_unsigned(data, offset)
        return Update(operation, receipt=receipt), offset
    if tag in (_TAG_BARRIER, _TAG_BARRIER_RECEIPT):
        count, offset = decode_unsigned(data, offset)
        registers = []
        for _ in range(count):
            register, offset = decode_unsigned(data, offset)
            registers.append(register)
        if tag == _TAG_BARRIER:
            return Barrier(tuple(registers)), offset
        receipt, offset = decode_unsigned(data, offset)
        return Barrier(tuple(registers), receipt=receipt), offset
    if tag == _TAG_RECEIPT:
        receipt, offset = decode_unsigned(data, offset)
        return Receipt(receipt), offset
    raise ValueError(f"unknown message tag {tag}")


def encode_stream(stream: Stream) -> bytes:
    out = bytearray(encode_declaration(stream.declaration))
    out += encode_unsigned(len(stream.messages))
    for message in stream.messages:
        out += _encode_message(message)
    return bytes(out)


def decode_stream(data: bytes) -> Stream:
    declaration, offset = decode_declaration(data, 0)
    count, offset = decode_unsigned(data, offset)
    messages: List[Message] = []
    for _ in range(count):
        message, offset = _decode_message(data, offset)
        messages.append(message)
    if offset != len(data):
        raise ValueError("trailing bytes after stream")
    return Stream(declaration, tuple(messages))


def encoded_length(stream: Stream) -> int:
    """Real encoded byte count of a full declaration + message envelope."""
    return len(encode_stream(stream))


def message_body_length(message: Message) -> int:
    """Byte count of one message inside the envelope body."""
    return len(_encode_message(message))
