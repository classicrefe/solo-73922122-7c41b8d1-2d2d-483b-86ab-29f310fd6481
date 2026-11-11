"""mailparse：从 MIME 邮件中筛选 UTF-8 CSV 附件。"""

from __future__ import annotations

from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart

import pytest

from app.csvparse import CsvValidationError
from app.mailparse import extract_csv_attachments


def _mail(subject: str = "t") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "instrument@lab"
    msg["To"] = "shared@lab"
    msg["Subject"] = subject
    return msg


def _attach_raw_part(
    msg,
    payload: bytes,
    content_type: str,
    filename: str,
) -> None:
    """绕过 contentmanager 直接挂载带指定 Content-Type/charset 的部件。"""
    maintype, _, subtype = content_type.partition("/")
    subtype = subtype.split(";")[0].strip()
    part = MIMEBase(maintype, subtype)
    part.set_payload(payload)
    part.replace_header("Content-Type", content_type)
    part.add_header("Content-Transfer-Encoding", "8bit")
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)


def test_multiple_csv_attachments_extracted_in_order():
    msg = _mail()
    msg.add_attachment(
        b"result_id,well_position,analyte,measured_at,measured_value\n"
        b"R1,A01,Pb,1,v\n",
        maintype="text", subtype="csv", filename="a.csv",
    )
    msg.add_attachment(
        b"result_id,well_position,analyte,measured_at,measured_value\n"
        b"R2,A02,Cd,2,w\n",
        maintype="text", subtype="csv", filename="b.csv",
    )
    attachments = extract_csv_attachments(bytes(msg))
    assert [a["filename"] for a in attachments] == ["a.csv", "b.csv"]
    assert all(a["data"].startswith(b"result_id") for a in attachments)


def test_non_csv_attachments_ignored():
    msg = _mail()
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application",
                       subtype="pdf", filename="report.pdf")
    msg.add_attachment(b"plain note", maintype="text",
                       subtype="plain", filename="note.txt")
    assert extract_csv_attachments(bytes(msg)) == []


def test_csv_suffix_with_non_text_maintype_ignored():
    # image/csv 这类异常组合不视为 CSV 附件，直接忽略而非拒绝整封
    msg = _legacy_mail()
    _attach_raw_part(msg, b"binary", "image/csv", "weird.csv")
    assert extract_csv_attachments(bytes(msg)) == []


def _legacy_mail() -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"] = "instrument@lab"
    msg["To"] = "shared@lab"
    msg["Subject"] = "legacy"
    return msg


def test_non_utf8_csv_rejects_whole_message():
    msg = _legacy_mail()
    payload = (
        "result_id,well_position,analyte,measured_at,measured_value\n"
        "R1,A01,铅,1,v\n"
    ).encode("gbk")
    _attach_raw_part(msg, payload, "text/csv; charset=gbk", "gbk.csv")
    with pytest.raises(CsvValidationError):
        extract_csv_attachments(bytes(msg))


def test_declared_utf16_charset_rejected():
    msg = _legacy_mail()
    payload = (
        "result_id,well_position,analyte,measured_at,measured_value\n"
        "R1,A01,Pb,1,v\n"
    ).encode("utf-16")
    _attach_raw_part(msg, payload, "text/csv; charset=utf-16", "u16.csv")
    with pytest.raises(CsvValidationError):
        extract_csv_attachments(bytes(msg))


def test_ascii_charset_accepted_as_utf8_compatible():
    msg = _mail()
    msg.add_attachment(
        b"result_id,well_position,analyte,measured_at,measured_value\n"
        b"R1,A01,Pb,1,v\n",
        maintype="text", subtype="csv", filename="ascii.csv",
    )
    attachments = extract_csv_attachments(bytes(msg))
    assert len(attachments) == 1


def test_multipart_without_filename_is_not_an_attachment():
    # multipart 中的文本主体部件没有文件名，按规则不计入 CSV 附件
    msg = _legacy_mail()
    part = MIMEBase("text", "plain")
    part.set_payload("just a body")
    msg.attach(part)
    assert extract_csv_attachments(bytes(msg)) == []
