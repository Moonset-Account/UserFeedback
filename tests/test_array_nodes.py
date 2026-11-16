"""
Regression tests for the fixed-width numeric array variant types.

Windows EVTX binary XML marks an array variant by OR-ing the scalar type
with 0x80 (for example, UNSIGNED_DWORD 0x08 -> UNSIGNED_DWORDARRAY 0x88).
These tests cover:

  * reading each fixed-width numeric array (signed and unsigned),
  * preserving the on-disk order and the representation of each element,
  * continuing to parse the following field from the correct offset, and
  * keeping the existing scalar and string-array variants unchanged.
"""
import struct

import Evtx.Nodes as e_nodes


def make_array(values, pack_char):
    """
    build the substitution payload for an array variant.

    Returns:
      bytes: the packed, fixed-width values in their given order.
    """
    return b"".join(struct.pack(pack_char, v) for v in values)


def read_substitution(type_, payload):
    """
    read an array variant the same way RootNode.substitutions does:
    the descriptor supplies the total payload size via the length argument.
    """
    return e_nodes.get_variant_value(payload, 0, None, None, type_, length=len(payload))


def test_unsigned_byte_array():
    payload = make_array([0, 1, 255, 128], "<B")
    node = read_substitution(e_nodes.NODE_TYPES.UNSIGNED_BYTEARRAY, payload)

    assert isinstance(node, e_nodes.UnsignedByteArrayTypeNode)
    assert node.length() == len(payload)
    assert [e.string() for e in node.elements()] == ["0", "1", "255", "128"]
    assert node.string() == (
        "<string>0</string>\n" "<string>1</string>\n" "<string>255</string>\n" "<string>128</string>\n"
    )


def test_signed_byte_array_keeps_sign():
    payload = make_array([0, 1, -1, -128, 127], "<b")
    node = read_substitution(e_nodes.NODE_TYPES.SIGNED_BYTEARRAY, payload)

    assert isinstance(node, e_nodes.SignedByteArrayTypeNode)
    assert [e.string() for e in node.elements()] == ["0", "1", "-1", "-128", "127"]


def test_unsigned_word_array():
    payload = make_array([0, 65535, 256], "<H")
    node = read_substitution(e_nodes.NODE_TYPES.UNSIGNED_WORDARRAY, payload)

    assert isinstance(node, e_nodes.UnsignedWordArrayTypeNode)
    assert node.length() == len(payload)
    assert [e.string() for e in node.elements()] == ["0", "65535", "256"]


def test_signed_word_array_keeps_sign():
    payload = make_array([1, -1, -32768, 32767], "<h")
    node = read_substitution(e_nodes.NODE_TYPES.SIGNED_WORDARRAY, payload)

    assert [e.string() for e in node.elements()] == ["1", "-1", "-32768", "32767"]


def test_unsigned_dword_array():
    payload = make_array([0, 0xFFFFFFFF, 0x01020304], "<I")
    node = read_substitution(e_nodes.NODE_TYPES.UNSIGNED_DWORDARRAY, payload)

    assert isinstance(node, e_nodes.UnsignedDwordArrayTypeNode)
    assert node.length() == len(payload)
    assert [e.string() for e in node.elements()] == ["0", str(0xFFFFFFFF), str(0x01020304)]


def test_signed_dword_array_keeps_sign():
    payload = make_array([1, -1, -2147483648, 2147483647], "<i")
    node = read_substitution(e_nodes.NODE_TYPES.SIGNED_DWORDARRAY, payload)

    assert [e.string() for e in node.elements()] == ["1", "-1", "-2147483648", "2147483647"]


def test_unsigned_qword_array():
    payload = make_array([0, 0xFFFFFFFFFFFFFFFF], "<Q")
    node = read_substitution(e_nodes.NODE_TYPES.UNSIGNED_QWORDARRAY, payload)

    assert isinstance(node, e_nodes.UnsignedQwordArrayTypeNode)
    assert node.length() == len(payload)
    assert [e.string() for e in node.elements()] == ["0", str(0xFFFFFFFFFFFFFFFF)]


def test_signed_qword_array_keeps_sign():
    payload = make_array([1, -1, -9223372036854775808, 9223372036854775807], "<q")
    node = read_substitution(e_nodes.NODE_TYPES.SIGNED_QWORDARRAY, payload)

    assert [e.string() for e in node.elements()] == [
        "1",
        "-1",
        "-9223372036854775808",
        "9223372036854775807",
    ]


def test_boolean_array_matches_scalar_boolean():
    payload = struct.pack("<iii", 1, 0, 5)
    node = read_substitution(e_nodes.NODE_TYPES.BOOLEANARRAY, payload)

    assert isinstance(node, e_nodes.BooleanArrayTypeNode)
    assert [e.string() for e in node.elements()] == ["True", "False", "True"]


def test_float_and_double_arrays():
    float_payload = struct.pack("<ff", 1.5, -2.25)
    float_node = read_substitution(e_nodes.NODE_TYPES.FLOATARRAY, float_payload)
    assert isinstance(float_node, e_nodes.FloatArrayTypeNode)
    assert [e.string() for e in float_node.elements()] == [
        e_nodes.FloatTypeNode(float_payload, 0, None, None).string(),
        e_nodes.FloatTypeNode(float_payload, 4, None, None).string(),
    ]

    double_payload = struct.pack("<dd", 1.5, -2.25)
    double_node = read_substitution(e_nodes.NODE_TYPES.DOUBLEARRAY, double_payload)
    assert isinstance(double_node, e_nodes.DoubleArrayTypeNode)
    assert [e.string() for e in double_node.elements()] == [
        e_nodes.DoubleTypeNode(double_payload, 0, None, None).string(),
        e_nodes.DoubleTypeNode(double_payload, 8, None, None).string(),
    ]


def test_inline_array_uses_word_length_prefix():
    # an inline (non-substitution) value carries a WORD size prefix
    payload = struct.pack("<H", 3 * 4) + make_array([10, 20, 30], "<I")
    node = e_nodes.get_variant_value(payload, 0, None, None, e_nodes.NODE_TYPES.UNSIGNED_DWORDARRAY)

    assert isinstance(node, e_nodes.UnsignedDwordArrayTypeNode)
    # the two prefix bytes plus the packed element bytes
    assert node.tag_length() == 2 + 12
    assert node.length() == 2 + 12
    assert [e.string() for e in node.elements()] == ["10", "20", "30"]


def test_empty_array_renders_nothing_and_has_zero_elements():
    node = read_substitution(e_nodes.NODE_TYPES.UNSIGNED_DWORDARRAY, b"")
    assert node.elements() == []
    assert node.string() == ""
    assert node.length() == 0


def test_parsing_continues_at_correct_offset_after_array():
    # simulate consecutive substitutions inside a RootNode buffer:
    # an unsigned dword array followed by a scalar unsigned dword.
    array_values = [7, 8, 9]
    array_payload = make_array(array_values, "<I")
    following_value = 0xDEADBEEF
    buffer_ = array_payload + struct.pack("<I", following_value)

    array_node = e_nodes.get_variant_value(
        buffer_, 0, None, None, e_nodes.NODE_TYPES.UNSIGNED_DWORDARRAY, length=len(array_payload)
    )

    # the next field begins exactly where the array ended
    next_offset = array_node.length()
    assert next_offset == len(array_payload)
    next_node = e_nodes.get_variant_value(buffer_, next_offset, None, None, e_nodes.NODE_TYPES.UNSIGNED_DWORD)
    assert isinstance(next_node, e_nodes.UnsignedDwordTypeNode)
    assert next_node.string() == str(following_value)


def test_array_type_constants_are_scalar_types_or_0x80():
    pairs = [
        (e_nodes.NODE_TYPES.SIGNED_BYTE, e_nodes.NODE_TYPES.SIGNED_BYTEARRAY),
        (e_nodes.NODE_TYPES.UNSIGNED_BYTE, e_nodes.NODE_TYPES.UNSIGNED_BYTEARRAY),
        (e_nodes.NODE_TYPES.SIGNED_WORD, e_nodes.NODE_TYPES.SIGNED_WORDARRAY),
        (e_nodes.NODE_TYPES.UNSIGNED_WORD, e_nodes.NODE_TYPES.UNSIGNED_WORDARRAY),
        (e_nodes.NODE_TYPES.SIGNED_DWORD, e_nodes.NODE_TYPES.SIGNED_DWORDARRAY),
        (e_nodes.NODE_TYPES.UNSIGNED_DWORD, e_nodes.NODE_TYPES.UNSIGNED_DWORDARRAY),
        (e_nodes.NODE_TYPES.SIGNED_QWORD, e_nodes.NODE_TYPES.SIGNED_QWORDARRAY),
        (e_nodes.NODE_TYPES.UNSIGNED_QWORD, e_nodes.NODE_TYPES.UNSIGNED_QWORDARRAY),
        (e_nodes.NODE_TYPES.FLOAT, e_nodes.NODE_TYPES.FLOATARRAY),
        (e_nodes.NODE_TYPES.DOUBLE, e_nodes.NODE_TYPES.DOUBLEARRAY),
        (e_nodes.NODE_TYPES.BOOLEAN, e_nodes.NODE_TYPES.BOOLEANARRAY),
    ]
    for scalar, array in pairs:
        assert array == (scalar | 0x80)


def test_scalar_variants_are_unchanged():
    # the unrelated scalar variants must keep their existing output
    assert e_nodes.UnsignedDwordTypeNode(struct.pack("<I", 1234), 0, None, None).string() == "1234"
    assert e_nodes.SignedDwordTypeNode(struct.pack("<i", -1234), 0, None, None).string() == "-1234"
    assert e_nodes.UnsignedQwordTypeNode(struct.pack("<Q", 42), 0, None, None).string() == "42"
    assert e_nodes.BooleanTypeNode(struct.pack("<i", 0), 0, None, None).string() == "False"


def test_wstring_array_rendering_is_unchanged():
    # the pre-existing string array remains the reference rendering style
    payload = "alpha\x00beta\x00".encode("utf-16-le")
    node = e_nodes.WstringArrayTypeNode(payload, 0, None, None, length=len(payload))
    assert node.string() == ("<string>alpha</string>\n" "<string>beta</string>\n")
    assert node.length() == len(payload)
