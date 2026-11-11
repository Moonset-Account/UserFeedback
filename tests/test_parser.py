"""解析器单元测试：编码主题、嵌套结构、坏附件错误捕获。"""
from mailacq.collector.parser import parse_message
from mailacq.generator.mime_builder import (
    alternative,
    broken_base64_attachment,
    build_message,
    csv_attachment,
    mixed,
    to_bytes,
)


def _simple(message_id="<t@x>", subject="实验通知 EXP-2026-0001", enc="B", extras=None):
    body = alternative("实验批号: EXP-2026-0001\n",
                       "<html>EXP-2026-0001</html>")
    msg = build_message(subject=subject, subject_encoding=enc,
                        from_="a@example.test", to="b@example.test",
                        date="Wed, 11 Nov 2026 09:30:00 +0800",
                        message_id=message_id, body=body, extra_parts=extras)
    return to_bytes(msg)


def test_encoded_subject_base64_decoded():
    parsed = parse_message(_simple(enc="B"))
    assert parsed.decoded_subject == "实验通知 EXP-2026-0001"
    assert "=?" in (parsed.raw_subject or "")
    assert "?b?" in (parsed.raw_subject or "").lower()


def test_encoded_subject_quoted_printable_decoded():
    raw = _simple(subject="Expedition EXP-2026-0002 notifiee", enc="Q")
    parsed = parse_message(raw)
    assert parsed.decoded_subject == "Expedition EXP-2026-0002 notifiee"
    assert "?q?" in (parsed.raw_subject or "").lower()


def test_nested_mixed_alternative_tree_and_part_ids():
    attachment = csv_attachment("读数.csv", "x\n1\n")
    raw = _simple(extras=[attachment])
    parsed = parse_message(raw)
    ids = set(parsed.all_parts.keys())
    # 1 = mixed, 1.1 = alternative, 1.1.1 plain, 1.1.2 html, 1.2 attachment
    assert {"1", "1.1", "1.1.1", "1.1.2", "1.2"}.issubset(ids)
    assert parsed.tree.content_type == "multipart/mixed"
    assert parsed.all_parts["1.1"].content_type == "multipart/alternative"
    assert parsed.all_parts["1.2"].is_attachment is True


def test_attachment_filename_encoded_word_decoded():
    attachment = csv_attachment("读数-EXP-2026-0001.csv", "a,1\n")
    parsed = parse_message(_simple(extras=[attachment]))
    node = parsed.all_parts["1.2"]
    assert node.filename == "读数-EXP-2026-0001.csv"
    assert "=?" in (node.raw_filename or "")


def test_broken_base64_attachment_records_part_error_but_message_survives():
    broken = broken_base64_attachment("坏.csv", "样品,读数\nA,9.9\n")
    parsed = parse_message(_simple(extras=[broken]))
    node = parsed.all_parts["1.2"]
    assert node.error is not None
    assert "Base64" in node.error or "Error" in node.error
    # 正文部分仍然可解析
    assert parsed.all_parts["1.1.1"].text is not None
