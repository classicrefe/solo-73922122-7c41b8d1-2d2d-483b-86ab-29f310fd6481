"""csvparse：时间解析、表头、行数上限与坏行校验测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.csvparse import (
    CSV_HEADER,
    MAX_ROWS_PER_MESSAGE,
    CsvValidationError,
    parse_attachment,
    parse_csv_text,
    parse_timestamp,
    validate_message_attachments,
)

# 2026-11-11T08:00:00Z 的真实整数秒，测试统一从 datetime 计算避免硬编码错误。
TS_2026 = int(
    datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc).timestamp()
)


def test_parse_unix_seconds_and_iso_utc():
    assert parse_timestamp("1762848000") == 1762848000
    assert parse_timestamp("  1762848000  ") == 1762848000
    assert parse_timestamp("2026-11-11T08:00:00Z") == TS_2026
    # 带 +08:00 偏移，换算为 UTC 秒
    assert parse_timestamp("2026-11-11T16:00:00+08:00") == TS_2026
    # 朴素时间按 UTC
    assert parse_timestamp("2026-11-11 08:00:00") == TS_2026
    # 小写 z 也接受
    assert parse_timestamp("2026-11-11T08:00:00z") == TS_2026


def test_parse_timestamp_failures():
    for bad in ("", "   ", "not-a-timestamp", "1.5", "2026-13-40T99:99:99Z"):
        with pytest.raises(CsvValidationError):
            parse_timestamp(bad)


def _csv(rows_text: str) -> bytes:
    return rows_text.encode("utf-8")


def test_parse_valid_csv_keeps_value_as_string():
    payload = _csv(
        ",".join(CSV_HEADER) + "\n"
        f"R1,A01,铅Pb,1000,0.12\n"
        f"R2,B02,镉Cd,2026-11-11T08:00:00Z,未检出\n"
    )
    rows = parse_attachment(payload, "a.csv")
    assert rows == [
        {"result_id": "R1", "well_position": "A01", "analyte": "铅Pb",
         "measured_at": 1000, "measured_value": "0.12"},
        {"result_id": "R2", "well_position": "B02", "analyte": "镉Cd",
         "measured_at": TS_2026, "measured_value": "未检出"},
    ]
    # 检测值始终是字符串
    assert all(isinstance(r["measured_value"], str) for r in rows)


def test_non_utf8_payload_rejected():
    # 0xff/0xfe 不可能是合法 UTF-8 起始字节
    payload = b"result_id,well_position,analyte,measured_at,measured_value\nR1,A01,\xff\xfe,1,v\n"
    with pytest.raises(CsvValidationError):
        parse_attachment(payload, "a.csv")


def test_header_must_match_exactly():
    payload = _csv("result_id,well_position,analyte,measured_at\nR1,A01,Pb,1\n")
    with pytest.raises(CsvValidationError):
        parse_csv_text(payload.decode(), "a.csv")

    payload = _csv("id,well_position,analyte,measured_at,measured_value\nR1,A01,Pb,1,v\n")
    with pytest.raises(CsvValidationError):
        parse_csv_text(payload.decode(), "a.csv")


def test_blank_required_field_rejected():
    payload = _csv(
        ",".join(CSV_HEADER) + "\n"
        "R1, ,铅Pb,1000,0.1\n"
    )
    with pytest.raises(CsvValidationError):
        parse_attachment(payload, "a.csv")


def test_wrong_column_count_rejected():
    payload = _csv(
        ",".join(CSV_HEADER) + "\n"
        "R1,A01,铅Pb,1000\n"  # 少一列
    )
    with pytest.raises(CsvValidationError):
        parse_attachment(payload, "a.csv")


def test_blank_lines_skipped_but_empty_file_rejected():
    payload = _csv(
        ",".join(CSV_HEADER) + "\n"
        "\n"
        "R1,A01,铅Pb,1000,v\n"
        "\n"
    )
    rows = parse_attachment(payload, "a.csv")
    assert len(rows) == 1

    with pytest.raises(CsvValidationError):
        parse_attachment(b"", "empty.csv")


def test_utf8_bom_in_header_is_accepted():
    payload = ("﻿" + ",".join(CSV_HEADER) + "\n"
               "R1,A01,铅Pb,1000,v\n").encode("utf-8")
    rows = parse_attachment(payload, "bom.csv")
    assert rows[0]["result_id"] == "R1"


def test_over_100_rows_per_message_rejected():
    lines = [",".join(CSV_HEADER)]
    lines += [f"R{i},A01,铅Pb,{1000 + i},v{i}" for i in range(MAX_ROWS_PER_MESSAGE + 1)]
    attachments = [{"filename": "big.csv", "data": "\n".join(lines).encode()}]
    with pytest.raises(CsvValidationError):
        validate_message_attachments(attachments)


def test_100_rows_accepted_and_split_across_attachments_sums():
    def make(prefix: str, count: int) -> bytes:
        lines = [",".join(CSV_HEADER)]
        lines += [f"{prefix}-{i},A01,铅Pb,{i},v" for i in range(count)]
        return "\n".join(lines).encode()

    attachments = [
        {"filename": "a.csv", "data": make("A", 60)},
        {"filename": "b.csv", "data": make("B", 40)},
    ]
    rows = validate_message_attachments(attachments)
    assert len(rows) == 100

    attachments[1]["data"] = make("B", 41)
    with pytest.raises(CsvValidationError):
        validate_message_attachments(attachments)


def test_no_attachments_means_zero_rows():
    assert validate_message_attachments([]) == []
