"""db：事务原子性、去重/冲突、UIDVALIDITY、锁与查询测试。"""

from __future__ import annotations

import pytest

from app import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    db.init_db(connection)
    yield connection
    connection.close()


def _row(rid="R1", value="0.1", t=1000, well="A01", analyte="Pb"):
    return {
        "result_id": rid,
        "well_position": well,
        "analyte": analyte,
        "measured_at": t,
        "measured_value": value,
    }


def test_pin_and_change_uidvalidity(conn):
    db.pin_uidvalidity(conn, "mb", 12345)
    assert db.get_uidvalidity(conn, "mb") == 12345

    # 相同值幂等
    db.pin_uidvalidity(conn, "mb", 12345)

    with pytest.raises(db.UidvalidityChangedError):
        db.pin_uidvalidity(conn, "mb", 99999)
    # 旧值保留
    assert db.get_uidvalidity(conn, "mb") == 12345


def test_commit_message_atomic_and_progress(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    inserted = db.commit_message(conn, "mb", uid=1, uidvalidity=100,
                                 rows=[_row("R1"), _row("R2", value="0.2")])
    assert inserted == 2
    assert db.get_completed_uids(conn, "mb") == {1}

    # 已提交邮件不重入
    again = db.commit_message(conn, "mb", uid=1, uidvalidity=100,
                              rows=[_row("R1"), _row("R9")])
    assert again == 0
    assert db.query_results(conn, ["R9"]) == []
    assert db.get_completed_uids(conn, "mb") == {1}


def test_same_id_same_content_dedup(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    db.commit_message(conn, "mb", 1, 100, [_row("R1")])
    inserted = db.commit_message(conn, "mb", 2, 100, [_row("R1"), _row("R2")])
    assert inserted == 1  # R1 去重，R2 新增
    assert db.get_completed_uids(conn, "mb") == {1, 2}


def test_same_id_different_content_rejects_whole_message(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    db.commit_message(conn, "mb", 1, 100, [_row("R1", value="0.1")])

    with pytest.raises(db.ContentConflictError):
        db.commit_message(conn, "mb", 2, 100,
                          [_row("R1", value="9.9"), _row("R3")])

    # 整封回滚：R3 不存在、UID 2 未完成
    assert db.query_results(conn, ["R3"]) == []
    assert db.get_completed_uids(conn, "mb") == {1}


def test_within_message_duplicate_ids_conflict(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    with pytest.raises(db.ContentConflictError):
        db.commit_message(
            conn, "mb", 1, 100,
            [_row("R1", value="0.1"), _row("R1", value="0.2")],
        )
    assert db.get_completed_uids(conn, "mb") == set()


def test_within_message_same_id_same_content_dedup(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    inserted = db.commit_message(
        conn, "mb", 1, 100, [_row("R1"), _row("R1")]
    )
    assert inserted == 1
    assert len(db.query_results(conn, ["R1"])) == 1


def test_commit_with_wrong_uidvalidity_rejected(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    with pytest.raises(db.UidvalidityChangedError):
        db.commit_message(conn, "mb", 1, 200, [_row("R1")])
    assert db.get_completed_uids(conn, "mb") == set()


def test_no_csv_message_recorded_with_zero_rows(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    inserted = db.commit_message(conn, "mb", 7, 100, rows=[])
    assert inserted == 0
    progress = db.mailbox_progress(conn, "mb")
    assert progress["completed_messages"] == 1
    assert progress["stored_rows"] == 0
    assert progress["last_completed_uid"] == 7
    assert 7 in db.get_completed_uids(conn, "mb")


def test_lock_excludes_second_holder_until_released(conn):
    token = db.acquire_lock(conn, "holder-a", stale_seconds=300)
    with pytest.raises(db.LockHeldError):
        db.acquire_lock(conn, "holder-b", stale_seconds=300)

    assert db.refresh_lock(conn, token) is True
    db.release_lock(conn, token)

    token2 = db.acquire_lock(conn, "holder-b", stale_seconds=300)
    assert token2 != token
    # 旧 token 已失效，不能刷新
    assert db.refresh_lock(conn, token) is False


def test_stale_lock_can_be_taken_over(tmp_path):
    connection = db.connect(str(tmp_path / "stale.db"))
    db.init_db(connection)
    token = db.acquire_lock(connection, "dead", stale_seconds=300)
    # 模拟持锁进程崩溃：把心跳回拨到过期阈值之外
    connection.execute(
        "UPDATE collect_lock SET heartbeat_at = ? WHERE token = ?",
        (db._now() - 301, token),
    )
    new_token = db.acquire_lock(connection, "alive", stale_seconds=300)
    assert new_token != token
    assert db.refresh_lock(connection, token) is False
    assert db.refresh_lock(connection, new_token) is True
    connection.close()


def test_query_results_order_and_missing(conn):
    db.pin_uidvalidity(conn, "mb", 100)
    db.commit_message(conn, "mb", 1, 100,
                      [_row("R1"), _row("R2"), _row("R3", value="未检出")])

    rows = db.query_results(conn, ["R3", "R1", "R3", "NOPE"])
    assert [r["result_id"] for r in rows] == ["R3", "R1"]  # 去重保序、缺失剔除
    assert rows[1]["measured_value"] == "0.1"
    assert rows[0]["measured_value"] == "未检出"
    assert isinstance(rows[0]["measured_at"], int)
    assert db.query_results(conn, []) == []


def test_two_connections_wal_and_busy_timeout(tmp_path):
    path = str(tmp_path / "wal.db")
    c1 = db.connect(path)
    db.init_db(c1)
    c2 = db.connect(path)
    # 两个连接同时可用，WAL 模式生效
    mode = c1.execute("PRAGMA journal_mode").fetchone()[0].lower()
    assert mode == "wal"

    db.pin_uidvalidity(c1, "mb", 1)
    # c2 能立刻读到 c1 已提交的数据
    assert db.get_uidvalidity(c2, "mb") == 1
    c1.close()
    c2.close()
