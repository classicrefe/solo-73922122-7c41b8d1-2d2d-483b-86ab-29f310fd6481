"""把指定 .eml 替换为含一条合法结果的 CSV 邮件（演示“修复坏邮件后续采”）。

用法: python -m tools.fix_fixture_message <path-to-eml>
"""

from __future__ import annotations

import sys

from email.message import EmailMessage


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法: python -m tools.fix_fixture_message <eml 路径>")
    path = sys.argv[1]
    message = EmailMessage()
    message["From"] = "instrument@lab.example"
    message["To"] = "shared@lab.example"
    message["Subject"] = "fixed batch04"
    payload = (
        "result_id,well_position,analyte,measured_at,measured_value\n"
        "R-0007,A01,铅Pb,1762848500,0.20\n"
    ).encode("utf-8")
    message.add_attachment(
        payload, maintype="text", subtype="csv",
        filename="batch04_fixed.csv",
    )
    with open(path, "wb") as handle:
        handle.write(bytes(message))
    print(f"已替换 {path}")


if __name__ == "__main__":
    main()
