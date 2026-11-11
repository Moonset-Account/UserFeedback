"""使用标准库 email 构造符合 RFC 的 MIME 邮件（全部为虚构内容）。

覆盖点：
- 编码主题（=?UTF-8?B?...?= 与 =?UTF-8?Q?...?=）
- multipart/alternative 与嵌套 multipart/mixed
- RFC 2231/2047 编码的附件文件名
- 故意损坏的附件传输编码
"""
from __future__ import annotations

from email import charset, header
from email.message import Message
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

CHARSET_UTF8 = charset.Charset("utf-8")


def encoded_subject(text: str, encoding: str = "B") -> header.Header:
    """构造编码主题。encoding='B' 为 base64，'Q' 为 quoted-printable。"""
    ch = charset.Charset("utf-8")
    ch.header_encoding = charset.BASE64 if encoding.upper() == "B" else charset.QP
    ch.body_encoding = charset.QP
    return header.Header(text, charset=ch)


def encoded_filename(name: str) -> str:
    """把附件文件名编码成 RFC 2047 encoded-word，用于测试文件名解码。"""
    return header.Header(name, charset=CHARSET_UTF8).encode()


def text_part(text: str, subtype: str = "plain", charset_name: str = "utf-8") -> MIMEText:
    return MIMEText(text, subtype, charset_name)


def alternative(text_body: str, html_body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg.attach(text_part(text_body, "plain"))
    msg.attach(text_part(html_body, "html"))
    return msg


def csv_attachment(filename: str, csv_text: str) -> MIMEApplication:
    part = MIMEApplication(csv_text.encode("utf-8"), _subtype="csv")
    part.add_header("Content-Disposition", "attachment", filename=encoded_filename(filename))
    return part


def mixed(*parts: Message) -> MIMEMultipart:
    msg = MIMEMultipart("mixed")
    for part in parts:
        msg.attach(part)
    return msg


def build_message(
    *,
    subject: str,
    subject_encoding: str,
    from_: str,
    to: str,
    date: str,
    message_id: str,
    body: Message,
    extra_parts: list[Message] | None = None,
) -> Message:
    """组装完整邮件。body 通常是 alternative 或 multipart。"""
    root = mixed(body, *(extra_parts or []))
    root["Subject"] = encoded_subject(subject, subject_encoding)
    root["From"] = from_
    root["To"] = to
    root["Date"] = date
    root["Message-ID"] = message_id
    return root


def broken_base64_attachment(filename: str, csv_text: str) -> MIMEBase:
    """构造一个声明 base64 但内容非法的附件（坏附件场景）。

    直接使用经典 MIMEBase API 手动写死负载，绕过高层 API 的自动编码。
    负载为纯 ASCII 但违反 base64 规则（含非法字符 '*'、长度畸形），
    使解析器在 get_payload(decode=True) 时稳定触发 binascii.Error；
    原始 CSV 文本仅作为注释保留，用于说明该附件“本应”包含的内容。
    """
    part = MIMEBase("text", "csv")
    # 每行 5 个字符（数据字符数 ≡ 1 (mod 4)），并夹带非法字符 '*'
    broken_payload = "a*b-c\nd-e*f\ng*h-i\n"
    part.set_payload(broken_payload)
    part.add_header("Content-Transfer-Encoding", "base64")
    part.add_header("Content-Disposition", "attachment",
                    filename=encoded_filename(filename))
    part.add_header("X-Intended-Content",
                    encoded_filename(csv_text[:80]))
    return part


def to_bytes(msg: Message) -> bytes:
    return msg.as_bytes()
