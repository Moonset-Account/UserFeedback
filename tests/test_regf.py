import struct

from dissect.regf import regf
from dissect.regf.c_regf import (
    REG_BINARY,
    REG_DWORD,
    REG_EXPAND_SZ,
    REG_MULTI_SZ,
    REG_NONE,
    REG_QWORD,
    REG_SZ,
)


def test_regf(system_hive):
    hive = regf.RegistryHive(system_hive)

    root = hive.root()

    assert len(list(root.subkeys())) == 17
    assert root.name == "ROOT"
    assert root.path == ""
    assert hive.open("Software") is root.subkey("Software") is root.subkey("software")

    key_path = "ControlSet001\\Control\\Lsa"
    lsa = hive.open(key_path)

    assert lsa.name == "Lsa"
    assert lsa.path == key_path
    assert lsa.subkey("JD").class_name == "cdebfed5"
    assert lsa.subkey("Skew1").class_name == "7db4e11c"
    assert lsa.subkey("GBG").class_name == "b185f3f2"
    assert lsa.subkey("Data").class_name == "a282942c"

    assert hive.open("ControlSet001\\Services\\Tcpip\\Parameters\\DNSRegisteredAdapters").class_name == "DynDRootClass"


def test_try_decode_sz_normal_value_with_varied_characters():
    # A normal REG_SZ containing ASCII, Latin Extended, CJK and an emoji
    # surrogate pair, properly terminated with a NULL code unit.
    value = "A\u0100B\u4e2d\U0001f600"
    data = value.encode("utf-16-le") + b"\x00\x00"

    assert regf.try_decode_sz(data) == value
    assert regf.parse_value(REG_SZ, data) == value
    # REG_EXPAND_SZ shares the same decoding path.
    assert regf.parse_value(REG_EXPAND_SZ, data) == value


def test_try_decode_sz_interior_double_null_does_not_terminate():
    # The bytes of 'A' (41 00) followed by U+0100 (00 01) contain a
    # b"\x00\x00" at an odd byte offset, spanning two adjacent characters.
    # This must not be mistaken for the terminating NULL code unit.
    value = "A\u0100\u0101C"
    data = value.encode("utf-16-le") + b"\x00\x00"

    assert data.count(b"\x00\x00") >= 2
    assert regf.try_decode_sz(data) == value


def test_try_decode_sz_padding_after_terminator_is_ignored():
    # Registry cells are padded; everything after the real terminator must
    # not leak into the decoded string, including non-zero padding bytes.
    value = "Path\\To\\Something \u4e2d"
    data = value.encode("utf-16-le") + b"\x00\x00" + b"\xab\xcd" * 4

    assert regf.try_decode_sz(data) == value


def test_try_decode_sz_ascii_string():
    value = "C:\\Windows\\System32\\drivers\\etc\\hosts"
    data = value.encode("utf-16-le") + b"\x00\x00"

    assert regf.try_decode_sz(data) == value


def test_try_decode_sz_empty_string():
    assert regf.try_decode_sz(b"") == ""
    assert regf.try_decode_sz(b"\x00\x00") == ""
    assert regf.parse_value(REG_SZ, b"") == ""
    assert regf.parse_value(REG_SZ, b"\x00\x00") == ""


def test_try_decode_sz_legacy_value_without_terminator():
    # Values written without a trailing terminator must still be read in
    # full instead of coming back empty.
    value = "Legacy\u0100Value\u4e2d"
    data = value.encode("utf-16-le")

    assert regf.try_decode_sz(data) == value
    assert regf.parse_value(REG_SZ, data) == value

    # Odd-length legacy data is padded to a whole code unit before decoding.
    truncated = data[:-1]
    assert regf.try_decode_sz(truncated) == truncated.ljust(len(truncated) + 1, b"\x00").decode("utf-16-le")


def test_parse_value_other_types_keep_existing_paths():
    # These value types must continue to be handled by their own decoding
    # paths and must not be affected by the REG_SZ string fix.
    assert regf.parse_value(REG_DWORD, struct.pack("<I", 0xDEADBEEF)) == 0xDEADBEEF
    assert regf.parse_value(REG_DWORD, b"") == 0
    assert regf.parse_value(REG_QWORD, struct.pack("<Q", 0x0123456789ABCDEF)) == 0x0123456789ABCDEF
    assert regf.parse_value(REG_QWORD, b"") == 0

    binary = b"\x00\x01\x02\x03\xff\xfe"
    assert regf.parse_value(REG_BINARY, binary) == binary
    assert regf.parse_value(REG_NONE, binary) == binary

    first = "first\u0100".encode("utf-16-le") + b"\x00\x00"
    second = "second".encode("utf-16-le") + b"\x00\x00"
    multi_sz = first + second + b"\x00\x00"
    assert regf.parse_value(REG_MULTI_SZ, multi_sz) == ["first\u0100", "second"]

    # Unknown / unimplemented types are returned as raw bytes.
    assert regf.parse_value(0x9, binary) == binary

