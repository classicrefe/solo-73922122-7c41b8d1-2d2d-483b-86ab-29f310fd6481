"""端到端：本地 IMAP 演示源 -> 采集器 -> SQLite -> 查询 API。

覆盖：多附件、无 CSV、坏行停止与续采、断线重连续采、去重、内容冲突、
UIDVALIDITY 变化、并发采集互斥、查询上限与 result_rows 可用性。
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from app import db
from app.collector import mailbox_key, run_collection
from app.config import ImapSettings
from app.imapclient import ImapClient, ImapFetchError
from tools import make_fixtures
from tools.imap_server import MessageStore, build_server


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ServerHandle:
    def __init__(self, mail_dir: str, drop_uid=None, uidvalidity=1700000000):
        self.port = _free_port()
        self.store = MessageStore(
            mail_dir=mail_dir,
            uidvalidity=uidvalidity,
            uid_start=1,
            drop_uid=drop_uid,
        )
        self.server = build_server(
            "127.0.0.1", self.port, self.store,
            username="lab", password="labpass", mailbox="INBOX",
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        _wait_port(self.port)
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _wait_port(port: int) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"测试 IMAP 服务器端口 {port} 未就绪")


@pytest.fixture()
def mail_dir(tmp_path: Path) -> Path:
    target = tmp_path / "mailbox"
    make_fixtures.write_fixtures(str(target))
    return target


@pytest.fixture()
def db_conn(tmp_path: Path):
    conn = db.connect(str(tmp_path / "collect.db"))
    db.init_db(conn)
    yield conn
    conn.close()


def _settings(port: int, batch: int = 200) -> ImapSettings:
    return ImapSettings(
        host="127.0.0.1",
        port=port,
        username="lab",
        password="labpass",
        mailbox="INBOX",
        use_ssl=False,
        connect_timeout_seconds=10,
        batch_max_messages=batch,
    )


# --------------------------------------------------------------------------- #
# 基础协议
# --------------------------------------------------------------------------- #


def test_imap_server_lists_and_fetches_fixture_uids(mail_dir):
    with _ServerHandle(str(mail_dir)) as handle:
        with ImapClient(_settings(handle.port)) as client:
            assert client.uidvalidity == 1700000000
            uids = client.list_uids()
            assert uids == [1, 2, 3, 4, 5, 6]
            message = client.fetch_message(1)
            assert b"batch01_a.csv" in message


def test_imap_bad_credentials_rejected(mail_dir):
    with _ServerHandle(str(mail_dir)) as handle:
        bad = ImapSettings(
            host="127.0.0.1", port=handle.port, username="lab",
            password="wrong", mailbox="INBOX", use_ssl=False,
            connect_timeout_seconds=5, batch_max_messages=10,
        )
        from app.imapclient import ImapConnectionError

        with pytest.raises(ImapConnectionError):
            with ImapClient(bad):
                pass


# --------------------------------------------------------------------------- #
# 采集主流程
# --------------------------------------------------------------------------- #


def test_collection_stops_at_bad_row_and_resumes_after_fix(mail_dir, db_conn):
    with _ServerHandle(str(mail_dir)) as handle:
        settings = _settings(handle.port)

        report = run_collection(db_conn, settings, holder="test")
        # UID 1/2/3 成功（含无 CSV 的 UID2），UID4 坏行停止
        assert report.status == "stopped"
        assert report.failed_uid == 4
        assert report.processed_uids == [1, 2, 3]
        assert report.messages_without_csv == 1
        assert report.remaining_pending_uids == [4, 5, 6]

        key = mailbox_key(settings)
        assert db.get_completed_uids(db_conn, key) == {1, 2, 3}
        assert db.total_results(db_conn) == 6  # 4 行(UID1) + 2 行(UID3)

        # 再次调用：已完成邮件不重入，仍然停在 UID4
        again = run_collection(db_conn, settings, holder="test")
        assert again.failed_uid == 4
        assert again.processed_uids == []
        assert db.total_results(db_conn) == 6

    # 修复坏邮件（替换为合法内容）后续采：UID4/5/6 全部完成，
    # UID6 与 UID1 同内容 -> 去重
    bad_path = mail_dir / "04_bad_row.eml"
    good_payload = (
        "result_id,well_position,analyte,measured_at,measured_value\n"
        "R-0007,A01,铅Pb,1762848500,0.20\n"
    ).encode()
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "instrument@lab.example"
    msg["To"] = "shared@lab.example"
    msg["Subject"] = "fixed"
    msg.add_attachment(good_payload, maintype="text", subtype="csv",
                       filename="batch04_fixed.csv")
    bad_path.write_bytes(bytes(msg))

    with _ServerHandle(str(mail_dir)) as handle:
        report = run_collection(db_conn, _settings(handle.port), holder="test")

    assert report.status == "completed"
    assert report.processed_uids == [4, 5, 6]
    assert report.duplicate_rows == 2  # UID6 的 R-0001/R-0002 去重
    assert report.inserted_rows == 2   # R-0007、R-0008
    assert db.total_results(db_conn) == 8


def test_no_csv_email_completed_without_rows(mail_dir, db_conn):
    with _ServerHandle(str(mail_dir)) as handle:
        run_collection(db_conn, _settings(handle.port), holder="test")
        key = "lab@127.0.0.1/INBOX"
        row = db_conn.execute(
            "SELECT has_csv, row_count FROM collected_uids "
            "WHERE mailbox_key=? AND uid=2", (key,)
        ).fetchone()
        assert row["has_csv"] == 0 and row["row_count"] == 0


def test_dedup_same_content_and_conflict_rejects(mail_dir, db_conn, tmp_path):
    with _ServerHandle(str(mail_dir)) as handle:
        report = run_collection(db_conn, _settings(handle.port), holder="test")
        assert report.status == "stopped"  # 停在 UID4
        # UID1 已入库 R-0001=0.12
        rows = db.query_results(db_conn, ["R-0001"])
        assert rows[0]["measured_value"] == "0.12"

    # 构造一封同 ID 异内容的新邮件放到独立邮箱目录
    conflict_dir = tmp_path / "conflict"
    make_fixtures.write_fixtures(str(conflict_dir))
    conflict_msg = (
        "result_id,well_position,analyte,measured_at,measured_value\n"
        "R-0001,A01,铅Pb,2026-11-11T08:00:00Z,9.99\n"
    ).encode()
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "instrument@lab.example"
    msg["To"] = "shared@lab.example"
    msg["Subject"] = "conflict"
    msg.add_attachment(conflict_msg, maintype="text", subtype="csv",
                       filename="conflict.csv")
    # 命名排在最后，成为新 UID
    (conflict_dir / "07_conflict.eml").write_bytes(bytes(msg))
    # 去掉坏行邮件，让采集能走到冲突邮件
    (conflict_dir / "04_bad_row.eml").unlink()

    with _ServerHandle(str(conflict_dir)) as handle:
        report = run_collection(db_conn, _settings(handle.port), holder="test")

    assert report.status == "stopped"
    assert report.failed_uid == 6  # 07 文件在 04 删除后排序为第 6 个 UID
    assert "内容不同" in report.reason
    # 冲突封整封回滚：进度停在它之前
    key = "lab@127.0.0.1/INBOX"
    assert 6 not in db.get_completed_uids(db_conn, key)
    # 旧数据原样保留
    assert db.query_results(db_conn, ["R-0001"])[0]["measured_value"] == "0.12"


def test_uidvalidity_change_rejects_and_keeps_old_data(mail_dir, db_conn):
    with _ServerHandle(str(mail_dir), uidvalidity=1700000000) as handle:
        run_collection(db_conn, _settings(handle.port), holder="test")

    # 服务器以新 UIDVALIDITY 重启（邮箱被重建的场景）
    with _ServerHandle(str(mail_dir), uidvalidity=999999999) as handle:
        with pytest.raises(db.UidvalidityChangedError):
            run_collection(db_conn, _settings(handle.port), holder="test")

    # 旧 UIDVALIDITY 与旧数据完整保留
    key = "lab@127.0.0.1/INBOX"
    assert db.get_uidvalidity(db_conn, key) == 1700000000
    assert db.total_results(db_conn) == 6


def test_connection_drop_stops_before_message_and_resumes(mail_dir, db_conn):
    # UID3 发送中途断线：UID1/2 先成功，UID3 不得部分入库
    with _ServerHandle(str(mail_dir), drop_uid=3) as handle:
        settings = _settings(handle.port)
        report = run_collection(db_conn, settings, holder="test")
        assert report.status == "stopped"
        assert report.failed_uid == 3
        assert report.processed_uids == [1, 2]
        key = mailbox_key(settings)
        assert db.get_completed_uids(db_conn, key) == {1, 2}

    # 网络/服务器恢复后重新连接（不再断线），从 UID3 续采直到坏行 UID4
    with _ServerHandle(str(mail_dir)) as handle:
        report = run_collection(db_conn, _settings(handle.port), holder="test")
        assert report.processed_uids == [3]
        assert report.failed_uid == 4
        assert db.get_completed_uids(db_conn, key) == {1, 2, 3}


def test_fetch_error_directly_wraps_as_fetch_error(mail_dir):
    with _ServerHandle(str(mail_dir), drop_uid=2) as handle:
        with ImapClient(_settings(handle.port)) as client:
            client.list_uids()
            with pytest.raises(ImapFetchError):
                client.fetch_message(2)


def test_batch_limit_leaves_remaining(mail_dir, db_conn):
    # 去掉坏行邮件，保证 5 封都可成功；batch 限制为 2
    (mail_dir / "04_bad_row.eml").unlink()
    with _ServerHandle(str(mail_dir)) as handle:
        settings = _settings(handle.port, batch=2)
        report = run_collection(db_conn, settings, holder="test")
        assert report.status == "completed"
        assert report.processed_uids == [1, 2]
        assert report.remaining_pending_uids == [3, 4, 5]

        second = run_collection(db_conn, settings, holder="test")
        assert second.processed_uids == [3, 4]
        third = run_collection(db_conn, settings, holder="test")
        assert third.processed_uids == [5]
        assert third.remaining_pending_uids == []


def test_concurrent_collections_are_serialized(mail_dir, db_conn):
    """两个线程同时采集同一邮箱：第二个直接被锁拒绝，无重复入库。"""
    with _ServerHandle(str(mail_dir)) as handle:
        settings = _settings(handle.port)
        results: list[Exception | object] = []

        def worker() -> None:
            # 每个线程独立连接，模拟两个 API 进程操作同一 SQLite 文件
            conn = db.connect(db_conn.execute(
                "SELECT file FROM pragma_database_list LIMIT 1"
            ).fetchone()[0])
            db.init_db(conn)
            try:
                results.append(run_collection(conn, settings, holder="worker"))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

    assert len(results) == 2
    assert any(isinstance(r, db.LockHeldError) for r in results), results
    report = next(r for r in results if not isinstance(r, Exception))
    assert report.processed_uids == [1, 2, 3]
    # 成功的那一轮只入库一次，无重复
    assert db.total_results(db_conn) == 6
    # 锁最终释放
    assert db.get_lock_state(db_conn) is None


def test_progress_tracking(mail_dir, db_conn):
    with _ServerHandle(str(mail_dir)) as handle:
        run_collection(db_conn, _settings(handle.port), holder="test")
    progress = db.mailbox_progress(db_conn, "lab@127.0.0.1/INBOX")
    assert progress["uidvalidity"] == 1700000000
    assert progress["completed_messages"] == 3
    assert progress["last_completed_uid"] == 3
    assert progress["stored_rows"] == 6
