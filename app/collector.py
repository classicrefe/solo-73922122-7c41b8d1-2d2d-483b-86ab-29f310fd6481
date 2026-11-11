"""采集编排：IMAP 取信 -> 整封校验 -> SQLite 原子提交。

关键保证
========
- UID 递增处理，已完成 UID 跳过（已提交邮件不重入）；
- 每封邮件的全部 CSV 校验通过后，结果与进度在同一事务提交；
- 断线、坏行、取信失败、内容冲突、UIDVALIDITY 变化 -> 停在该封之前，
  不写入、不推进进度，下次调用从该 UID 继续；
- 采集期间持有应用级心跳锁，两个 API 进程同时调用时第二个直接被拒，
  因此不会漏信、重复入库或越过失败邮件。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import db
from .config import (
    LOCK_HEARTBEAT_SECONDS,
    LOCK_STALE_SECONDS,
    ImapSettings,
)
from .csvparse import CsvValidationError, validate_message_attachments
from .imapclient import (
    ImapClient,
    ImapFetchError,
)
from .mailparse import extract_csv_attachments

logger = logging.getLogger("lab.collector")


@dataclass
class CollectReport:
    status: str  # completed | stopped | uidvalidity_rejected
    mailbox: str
    uidvalidity: Optional[int]
    processed_uids: List[int] = field(default_factory=list)
    inserted_rows: int = 0
    duplicate_rows: int = 0
    messages_without_csv: int = 0
    failed_uid: Optional[int] = None
    reason: Optional[str] = None
    remaining_pending_uids: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "processed_uids": self.processed_uids,
            "processed_count": len(self.processed_uids),
            "inserted_rows": self.inserted_rows,
            "duplicate_rows": self.duplicate_rows,
            "messages_without_csv": self.messages_without_csv,
            "failed_uid": self.failed_uid,
            "reason": self.reason,
            "remaining_pending_uids": self.remaining_pending_uids,
        }


class _Heartbeat(threading.Thread):
    """后台刷新采集锁心跳；锁丢失时置位事件，主循环据此中止。"""

    def __init__(self, conn: sqlite3.Connection, token: str) -> None:
        super().__init__(daemon=True, name="collect-lock-heartbeat")
        self._conn = conn
        self._token = token
        self._stop = threading.Event()
        self.lock_lost = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(LOCK_HEARTBEAT_SECONDS):
            try:
                if not db.refresh_lock(self._conn, self._token):
                    self.lock_lost.set()
                    return
            except sqlite3.Error:
                # 数据库瞬时问题不立即判死，下一轮再试；真正失败会由
                # 提交事务暴露并停在当前邮件之前。
                logger.exception("心跳刷新失败，将在下一轮重试")

    def stop(self) -> None:
        self._stop.set()


def mailbox_key(settings: ImapSettings) -> str:
    return f"{settings.username}@{settings.host}/{settings.mailbox}"


def run_collection(
    conn: sqlite3.Connection, settings: ImapSettings, holder: str
) -> CollectReport:
    """执行一轮采集；调用方负责把异常映射为 HTTP 状态码。"""
    settings.validate()
    key = mailbox_key(settings)
    token = db.acquire_lock(conn, holder, LOCK_STALE_SECONDS)

    heartbeat = _Heartbeat(conn, token)
    heartbeat.start()
    client: Optional[ImapClient] = None
    try:
        client = ImapClient(settings)
        client.connect()
        uidvalidity = client.uidvalidity
        assert uidvalidity is not None

        # 首次登记或校验 UIDVALIDITY；变化时整体拒绝并保留旧数据。
        db.pin_uidvalidity(conn, key, uidvalidity)

        completed = db.get_completed_uids(conn, key)
        all_uids = client.list_uids()
        pending = [uid for uid in all_uids if uid not in completed]
        pending.sort()  # UID 递增
        capped = pending[: settings.batch_max_messages]

        report = CollectReport(
            status="completed",
            mailbox=key,
            uidvalidity=uidvalidity,
            remaining_pending_uids=pending[settings.batch_max_messages :],
        )

        for uid in capped:
            if heartbeat.lock_lost.is_set():
                report.status = "stopped"
                report.failed_uid = uid
                report.reason = "采集锁心跳丢失，已停止以避免并发写入"
                report.remaining_pending_uids = [uid] + [
                    u for u in capped if u > uid
                ]
                break

            try:
                raw_message = client.fetch_message(uid)
                attachments = extract_csv_attachments(raw_message)
                try:
                    rows = validate_message_attachments(attachments)
                except CsvValidationError as exc:
                    raise CsvValidationError(f"UID {uid}: {exc}") from exc
                inserted = db.commit_message(conn, key, uid, uidvalidity, rows)
            except (
                ImapFetchError,
                CsvValidationError,
                db.ContentConflictError,
                db.UidvalidityChangedError,
            ) as exc:
                # 停在该封之前：本封无任何写入，进度未推进。
                report.status = "stopped"
                report.failed_uid = uid
                report.reason = str(exc)
                report.remaining_pending_uids = [uid] + [
                    u for u in capped if u > uid
                ]
                logger.warning("采集在 UID %s 处停止: %s", uid, exc)
                break

            report.processed_uids.append(uid)
            if not rows:
                report.messages_without_csv += 1
            report.inserted_rows += inserted
            report.duplicate_rows += len(rows) - inserted
            logger.info(
                "UID %s 提交完成: 封内 %s 行, 新增 %s 行, 去重 %s 行",
                uid,
                len(rows),
                inserted,
                len(rows) - inserted,
            )

        return report
    except db.UidvalidityChangedError:
        # 不吞细节：交给上层映射 409，旧数据保持不变。
        raise
    finally:
        heartbeat.stop()
        if client is not None:
            client.close()
        try:
            db.release_lock(conn, token)
        except sqlite3.Error:
            logger.exception("释放采集锁失败")
