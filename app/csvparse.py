"""CSV 附件解析与整封校验。

规则
====
- 仅接受 UTF-8 文本 CSV；表头固定为现有结果字段
  ``result_id,well_position,analyte,measured_at,measured_value``；
- 时间解析为 UTC 整数秒：支持纯整数 Unix 秒，或 ISO-8601
  （``2026-11-11T08:30:00Z`` / ``+08:00``，无时区则按 UTC）；
- 检测值一律保留为字符串，不做任何数值转换（如 ``"0.12"``、``"未检出"``）；
- 一封邮件的全部 CSV 附件合计数据行最多 100 行；
- 任一行不合法即抛 ``CsvValidationError``，由采集器放弃整封、不推进进度。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

CSV_HEADER = ["result_id", "well_position", "analyte", "measured_at", "measured_value"]
MAX_ROWS_PER_MESSAGE = 100


class CsvValidationError(ValueError):
    """CSV 内容不合法（编码、表头、行数、字段或时间解析问题）。"""


def parse_timestamp(raw: str) -> int:
    """把时间字段解析为 UTC 整数秒。

    接受：
    - 纯整数（可带正负号）：直接作为 Unix 秒；
    - ISO-8601 字符串：支持 ``Z`` 后缀与显式偏移；朴素时间按 UTC。
    """
    text = raw.strip()
    if not text:
        raise CsvValidationError("measured_at 为空")

    # 纯整数 Unix 时间戳（拒绝 1.5 这类浮点写法，保证整数秒语义）。
    if text.lstrip("+-").isdigit():
        return int(text)

    iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise CsvValidationError(f"无法解析时间 {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def decode_csv_payload(data: bytes, source: str) -> str:
    """只接受 UTF-8（严格），拒绝其他编码与伪 UTF-8 字节。"""
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CsvValidationError(f"{source} 不是合法 UTF-8 文本: {exc}") from exc


def parse_csv_text(text: str, source: str) -> List[Dict[str, Any]]:
    """解析单个 CSV 文本，返回行字典列表（不含表头）。"""
    # 兼容带 BOM 的 UTF-8（Excel 另存为常见），避免首列名被污染成
    # "﻿result_id" 而导致表头校验失败。
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        all_rows = list(reader)
    except csv.Error as exc:
        raise CsvValidationError(f"{source} CSV 解析失败: {exc}") from exc

    # 去掉尾部由空行产生的空记录，中间的空记录仍按坏行处理（字段数不符）。
    while all_rows and not any(cell.strip() for cell in all_rows[-1]):
        all_rows.pop()
    if not all_rows:
        raise CsvValidationError(f"{source} 只有空内容，缺少表头")

    header = [cell.strip() for cell in all_rows[0]]
    if header != CSV_HEADER:
        raise CsvValidationError(
            f"{source} 表头必须为 {','.join(CSV_HEADER)}，实际为 {','.join(header)}"
        )

    parsed_rows: List[Dict[str, Any]] = []
    for line_no, cells in enumerate(all_rows[1:], start=2):
        if not any(cell.strip() for cell in cells):
            # 数据区中的纯空行直接跳过，不计入 100 行上限。
            continue
        if len(cells) != len(CSV_HEADER):
            raise CsvValidationError(
                f"{source} 第 {line_no} 行字段数为 {len(cells)}，"
                f"应为 {len(CSV_HEADER)}"
            )
        result_id, well_position, analyte, measured_at_raw, measured_value = (
            c.strip() for c in cells
        )
        if not result_id:
            raise CsvValidationError(f"{source} 第 {line_no} 行 result_id 为空")
        if not well_position:
            raise CsvValidationError(f"{source} 第 {line_no} 行 well_position 为空")
        if not analyte:
            raise CsvValidationError(f"{source} 第 {line_no} 行 analyte 为空")

        parsed_rows.append(
            {
                "result_id": result_id,
                "well_position": well_position,
                "analyte": analyte,
                "measured_at": parse_timestamp(measured_at_raw),
                # 检测值保留字符串原样（仅去除 CSV 字段边界，不做数值化）。
                "measured_value": measured_value,
            }
        )
    return parsed_rows


def parse_attachment(data: bytes, filename: str) -> List[Dict[str, Any]]:
    """解析一个已确认的 CSV 附件负载。"""
    source = f"附件 {filename or '<unnamed>'}"
    return parse_csv_text(decode_csv_payload(data, source), source)


def validate_message_attachments(
    attachments: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """汇总并校验一封邮件的全部 CSV 附件。

    入参为 ``{"filename": str, "data": bytes}`` 列表（已由邮件层筛出
    且仅含 UTF-8 可解码的 ``.csv`` 附件）；无附件时返回空列表，
    调用方据此把该邮件记为“无 CSV 完成”。
    """
    rows: List[Dict[str, Any]] = []
    for attachment in attachments:
        rows.extend(
            parse_attachment(attachment["data"], attachment.get("filename", ""))
        )
    if len(rows) > MAX_ROWS_PER_MESSAGE:
        raise CsvValidationError(
            f"单封邮件 CSV 数据行合计 {len(rows)} 行，"
            f"超过上限 {MAX_ROWS_PER_MESSAGE} 行"
        )
    return rows
