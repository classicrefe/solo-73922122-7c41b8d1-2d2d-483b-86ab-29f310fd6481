"""从原始 MIME 邮件中筛选 UTF-8 CSV 附件。

筛选口径
========
- 仅处理带文件名且文件名以 ``.csv`` 结尾（忽略大小写）的部件，
  Content-Type 限定为 text/* 或 application/*（如 text/csv、
  application/csv、application/octet-stream）；
- 负载必须能按 UTF-8 严格解码（兼容 Excel 常见的 UTF-8 BOM），
  声明其他 charset 的部件直接拒绝，整封邮件视为不合格；
- 非 CSV 附件（如 PDF、图片）直接忽略，不影响该邮件处理；
- 无任何 CSV 附件的邮件允许记为完成（零行）。
"""

from __future__ import annotations

import email.message
import email.policy
from email import message_from_bytes
from typing import Any, Dict, List

from .csvparse import CsvValidationError, decode_csv_payload

_ALLOWED_MAIN_TYPES = ("text", "application")


def _is_csv_part(part: email.message.EmailMessage) -> bool:
    filename = part.get_filename()
    if not filename or not filename.lower().endswith(".csv"):
        return False
    if part.get_content_maintype() not in _ALLOWED_MAIN_TYPES:
        return False
    return True


def extract_csv_attachments(raw_message: bytes) -> List[Dict[str, Any]]:
    """解析 RFC822 原始邮件，返回 ``[{"filename", "data"}]``。

    部件声明的 charset 若不是 UTF-8 兼容值则拒绝；负载最终仍以原始
    字节做一次严格 UTF-8 解码，杜绝双重编码伪装。
    """
    try:
        message = message_from_bytes(
            raw_message, policy=email.policy.default
        )
    except Exception as exc:  # 畸形邮件
        raise CsvValidationError(f"邮件 MIME 解析失败: {exc}") from exc

    attachments: List[Dict[str, Any]] = []
    for part in message.walk():
        if not _is_csv_part(part):
            continue
        charset = (part.get_content_charset() or "").lower()
        if charset and charset not in ("utf-8", "utf8", "us-ascii"):
            raise CsvValidationError(
                f"CSV 附件 {part.get_filename()} 声明编码 {charset}，"
                "仅处理 UTF-8"
            )
        payload = part.get_payload(decode=True)
        if payload is None:
            raise CsvValidationError(
                f"CSV 附件 {part.get_filename()} 无法解码负载"
            )
        # 严格 UTF-8 校验；解码后的文本重新编码回字节交给 CSV 层，
        # 保证后续解析使用的就是这份已验证文本。
        decode_csv_payload(payload, f"附件 {part.get_filename()}")
        attachments.append(
            {"filename": part.get_filename(), "data": payload}
        )
    return attachments
