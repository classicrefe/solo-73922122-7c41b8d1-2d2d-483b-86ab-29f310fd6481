"""IMAP 采集客户端（标准库 imaplib 封装）。

只依赖 IMAP4rev1 基础命令，连接参数全部来自环境配置：

    LOGIN -> SELECT（取 UIDVALIDITY）-> UID SEARCH ALL
           -> UID FETCH <uid> (RFC822)

取信按 UID 递增；任何连接/取信失败都向上抛出明确异常，由采集器
停在当前邮件之前，绝不越过失败邮件推进进度。
"""

from __future__ import annotations

import imaplib
import re
import socket
from typing import List, Optional

from .config import ImapSettings


class ImapConnectionError(RuntimeError):
    """连接、登录或选择邮箱失败。"""


class ImapFetchError(RuntimeError):
    """取信（SEARCH/FETCH）过程中失败，包括断线。"""


_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)", re.IGNORECASE)
_UID_RE = re.compile(rb"UID\s+(\d+)", re.IGNORECASE)


class ImapClient:
    """单次采集周期内使用的连接对象，建议配合 with 使用。"""

    def __init__(self, settings: ImapSettings) -> None:
        self._settings = settings
        self._conn: Optional[imaplib.IMAP4] = None
        self.uidvalidity: Optional[int] = None

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        settings = self._settings
        socket.setdefaulttimeout(settings.connect_timeout_seconds)
        try:
            if settings.use_ssl:
                conn = imaplib.IMAP4_SSL(
                    settings.host,
                    settings.port,
                    timeout=settings.connect_timeout_seconds,
                )
            else:
                conn = imaplib.IMAP4(
                    settings.host,
                    settings.port,
                    timeout=settings.connect_timeout_seconds,
                )
            conn.login(settings.username, settings.password)
        except (OSError, imaplib.IMAP4.error, socket.gaierror) as exc:
            raise ImapConnectionError(f"IMAP 连接/登录失败: {exc}") from exc

        self._conn = conn
        self.uidvalidity = self._select_mailbox(settings.mailbox)

    def _select_mailbox(self, mailbox: str) -> int:
        assert self._conn is not None
        try:
            typ, data = self._conn.select(mailbox, readonly=True)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapConnectionError(f"选择邮箱 {mailbox!r} 失败: {exc}") from exc
        if typ != "OK":
            detail = data[0].decode("utf-8", "replace") if data else "未知错误"
            raise ImapConnectionError(f"选择邮箱 {mailbox!r} 被拒: {detail}")

        # imaplib 会把 SELECT 的未标记响应解析进 response('UIDVALIDITY')，
        # 同时兼容原始响应正则，防止不同服务端格式差异。
        parsed = self._conn.response("UIDVALIDITY")
        if parsed and parsed[1]:
            try:
                return int(parsed[1][0])
            except (ValueError, IndexError):
                pass
        for line in self._conn.untagged_responses.get("OK", []) or []:
            match = _UIDVALIDITY_RE.search(line)
            if match:
                return int(match.group(1))
        raise ImapConnectionError("服务端未返回 UIDVALIDITY，无法安全采集")

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        finally:
            self._conn = None

    # ------------------------------------------------------------------ #
    # 取信
    # ------------------------------------------------------------------ #

    def list_uids(self) -> List[int]:
        """返回邮箱中全部邮件的 UID，按数值递增。"""
        assert self._conn is not None
        try:
            typ, data = self._conn.uid("SEARCH", None, "ALL")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapFetchError(f"UID SEARCH 失败: {exc}") from exc
        if typ != "OK":
            raise ImapFetchError(f"UID SEARCH 返回非 OK: {typ}")
        raw = data[0] if data and data[0] else b""
        return sorted(int(part) for part in raw.split())

    def fetch_message(self, uid: int) -> bytes:
        """按 UID 拉取整封 RFC822 邮件；断线等失败统一抛 ImapFetchError。"""
        assert self._conn is not None
        try:
            typ, data = self._conn.uid("FETCH", str(uid), "(RFC822)")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapFetchError(f"UID {uid} 取信失败: {exc}") from exc
        if typ != "OK":
            raise ImapFetchError(f"UID {uid} FETCH 返回非 OK: {typ}")

        for item in data or ():
            if not isinstance(item, tuple):
                continue
            envelope, payload = item
            # 防御性校验：确保拿到的确实是请求的 UID，避免错信入库。
            match = _UID_RE.search(envelope if isinstance(envelope, bytes) else b"")
            if match and int(match.group(1)) != uid:
                raise ImapFetchError(
                    f"UID 错位: 请求 {uid}，服务端返回 {match.group(1).decode()}"
                )
            return payload
        raise ImapFetchError(f"UID {uid} 的邮件负载缺失（可能已被删除或连接中断）")
