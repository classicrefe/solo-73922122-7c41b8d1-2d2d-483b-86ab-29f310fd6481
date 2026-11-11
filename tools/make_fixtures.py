"""生成演示与测试用的 .eml 邮件夹具（标准库 email 构造）。

每封邮件写入目标目录，文件名前缀决定排序与 UID（从 1 起连续）：

  01_multi_attach.eml       两个 CSV 附件（多附件演示），各含合法结果
  02_no_csv.eml             只有纯文本附件，无 CSV -> 记完成、零行
  03_good_single.eml        单个 CSV，含 ISO-8601 时间与字符串检测值
  04_bad_row.eml            CSV 含坏行（时间无法解析）-> 整封拒绝、停止
  05_after_failure.eml      坏邮件之后的正常邮件，用于验证续采不越过失败
  06_duplicate.eml          与 01 同结果 ID 同内容 -> 去重入库

用法:
    python -m tools.make_fixtures --out /tmp/mailbox
"""

from __future__ import annotations

import argparse
import csv
import io
import os
from email.message import EmailMessage

HEADER = "result_id,well_position,analyte,measured_at,measured_value"


def csv_text(rows: list[list[str]], header: str = HEADER) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header.split(","))
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def add_csv(message: EmailMessage, filename: str, payload: bytes) -> None:
    message.add_attachment(
        payload,
        maintype="text",
        subtype="csv",
        filename=filename,
    )


def add_text(message: EmailMessage, filename: str, body: str) -> None:
    message.add_attachment(
        body.encode("utf-8"),
        maintype="text",
        subtype="plain",
        filename=filename,
    )


def make_message(subject: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "instrument@lab.example"
    message["To"] = "shared-mailbox@lab.example"
    message["Subject"] = subject
    message["Date"] = "Wed, 11 Nov 2026 08:00:00 +0000"
    return message


def build_fixtures() -> dict[str, bytes]:
    fixtures: dict[str, bytes] = {}

    # 01: 多附件，两个 CSV 合计 4 行（未超每封 100 行上限）。
    msg = make_message("仪器结果批次 01（多附件）")
    add_csv(
        msg,
        "batch01_a.csv",
        csv_text([
            ["R-0001", "A01", "铅Pb", "2026-11-11T08:00:00Z", "0.12"],
            ["R-0002", "A01", "铅Pb", "1762848060", "0.15"],
        ]),
    )
    add_csv(
        msg,
        "batch01_b.csv",
        csv_text([
            ["R-0003", "B02", "镉Cd", "2026-11-11 08:05:00+00:00", "0.05"],
            ["R-0004", "B02", "镉Cd", "2026-11-11T16:10:00+08:00", "未检出"],
        ]),
    )
    fixtures["01_multi_attach.eml"] = bytes(msg)

    # 02: 无 CSV 附件 -> 允许记为完成。
    msg = make_message("仪器状态通知（无 CSV）")
    add_text(msg, "status.txt", "仪器自检完成，无结果文件。\n")
    fixtures["02_no_csv.eml"] = bytes(msg)

    # 03: 单 CSV，整数秒时间 + 字符串检测值。
    msg = make_message("仪器结果批次 03")
    add_csv(
        msg,
        "batch03.csv",
        csv_text([
            ["R-0005", "C03", "汞Hg", "1762848300", "0.007"],
            ["R-0006", "D04", "砷As", "1762848360", "1.10"],
        ]),
    )
    fixtures["03_good_single.eml"] = bytes(msg)

    # 04: 坏行（时间列既不是整数也不是合法 ISO-8601）-> 整封拒绝。
    msg = make_message("仪器结果批次 04（含坏行）")
    add_csv(
        msg,
        "batch04_bad.csv",
        csv_text([
            ["R-0007", "A01", "铅Pb", "not-a-timestamp", "0.20"],
        ]),
    )
    fixtures["04_bad_row.eml"] = bytes(msg)

    # 05: 失败邮件之后的正常邮件，修复 04 后应被续采到。
    msg = make_message("仪器结果批次 05（失败点之后）")
    add_csv(
        msg,
        "batch05.csv",
        csv_text([
            ["R-0008", "A02", "铅Pb", "1762848420", "0.31"],
        ]),
    )
    fixtures["05_after_failure.eml"] = bytes(msg)

    # 06: 与 01 完全相同的两个结果 ID + 内容 -> 去重，不新增行。
    msg = make_message("仪器结果批次 06（重复数据）")
    add_csv(
        msg,
        "batch06_dup.csv",
        csv_text([
            ["R-0001", "A01", "铅Pb", "2026-11-11T08:00:00Z", "0.12"],
            ["R-0002", "A01", "铅Pb", "1762848060", "0.15"],
        ]),
    )
    fixtures["06_duplicate.eml"] = bytes(msg)

    return fixtures


def write_fixtures(out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for name, payload in build_fixtures().items():
        path = os.path.join(out_dir, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 IMAP 演示邮件夹具")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()
    for path in write_fixtures(args.out):
        print(path)


if __name__ == "__main__":
    main()
