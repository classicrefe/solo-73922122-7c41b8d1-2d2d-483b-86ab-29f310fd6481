"""SQLite 持久化：结果、邮箱 UIDVALIDITY、已完成 UID 与采集锁。

并发模型
========
两个 API 进程可能同时操作同一个数据库文件：

- 启用 WAL，读写不互相阻塞，查询接口在采集进行中也能读到已提交数据；
- ``busy_timeout`` 让写锁竞争时等待而不是立即报错；
- 采集互斥用 ``collect_lock`` 单行锁实现（应用级锁 + 心跳），
  保证同一时刻只有一个进程采集同一邮箱；
- 每封邮件在一个 ``BEGIN IMMEDIATE`` 事务内同时写入结果与进度，
  要么整封可见、要么完全不留痕。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    result_id     TEXT PRIMARY KEY,
    well_position TEXT NOT NULL,
    analyte       TEXT NOT NULL,
    measured_at   INTEGER NOT NULL,
    measured_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_key  TEXT PRIMARY KEY,
    uidvalidity  INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS collected_uids (
    mailbox_key  TEXT NOT NULL,
    uid          INTEGER NOT NULL,
    uidvalidity  INTEGER NOT NULL,
    has_csv      INTEGER NOT NULL,
    row_count    INTEGER NOT NULL,
    collected_at INTEGER NOT NULL,
    PRIMARY KEY (mailbox_key, uid)
);

CREATE TABLE IF NOT EXISTS collect_lock (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    token        TEXT NOT NULL,
    holder       TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL
);
"""


class ContentConflictError(Exception):
    """同一结果 ID 已存在但内容不同，整封邮件必须拒绝。"""


class UidvalidityChangedError(Exception):
    """邮箱 UIDVALIDITY 与已保存值不一致，拒绝继续采集。"""


class LockHeldError(Exception):
    """已有其他采集进程持锁。"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接。

    使用 ``isolation_level=None`` 手动管理事务，避免 sqlite3 模块的
    隐式事务与显式 ``BEGIN IMMEDIATE`` 互相干扰。
    """
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# 采集互斥锁
# --------------------------------------------------------------------------- #


def _now() -> int:
    return int(time.time())


def get_lock_state(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM collect_lock WHERE id = 1").fetchone()
    if row is None:
        return None
    return dict(row)


def acquire_lock(
    conn: sqlite3.Connection, holder: str, stale_seconds: int
) -> str:
    """尝试获取单行采集锁，返回 token；失败抛 LockHeldError。

    崩溃残留的锁（心跳超过 stale_seconds）可被接管；活跃持有者的锁
    绝不抢占，从而保证两个进程不会同时采集。
    """
    token = uuid.uuid4().hex
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT token, heartbeat_at FROM collect_lock WHERE id = 1"
        ).fetchone()
        if row is not None and now - row["heartbeat_at"] < stale_seconds:
            conn.execute("ROLLBACK")
            raise LockHeldError("已有采集任务正在运行")
        conn.execute(
            """
            INSERT INTO collect_lock (id, token, holder, started_at, heartbeat_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                token = excluded.token,
                holder = excluded.holder,
                started_at = excluded.started_at,
                heartbeat_at = excluded.heartbeat_at
            """,
            (token, holder, now, now),
        )
        conn.execute("COMMIT")
    except Exception:
        # rollback 幂等：COMMIT 成功后再 ROLLBACK 会报错，忽略即可。
        _safe_rollback(conn)
        raise
    return token


def refresh_lock(conn: sqlite3.Connection, token: str) -> bool:
    """刷新心跳；锁已丢失（被接管）时返回 False。"""
    cur = conn.execute(
        "UPDATE collect_lock SET heartbeat_at = ? WHERE id = 1 AND token = ?",
        (_now(), token),
    )
    return cur.rowcount == 1


def release_lock(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM collect_lock WHERE id = 1 AND token = ?", (token,))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        _safe_rollback(conn)
        raise


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------- #
# 邮箱状态与进度
# --------------------------------------------------------------------------- #


def get_uidvalidity(conn: sqlite3.Connection, mailbox_key: str) -> Optional[int]:
    row = conn.execute(
        "SELECT uidvalidity FROM mailboxes WHERE mailbox_key = ?",
        (mailbox_key,),
    ).fetchone()
    return None if row is None else int(row["uidvalidity"])


def pin_uidvalidity(
    conn: sqlite3.Connection, mailbox_key: str, uidvalidity: int
) -> None:
    """首次见到邮箱时登记 UIDVALIDITY；已存在则必须一致，否则拒绝。"""
    with transaction(conn):
        row = conn.execute(
            "SELECT uidvalidity FROM mailboxes WHERE mailbox_key = ?",
            (mailbox_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mailboxes (mailbox_key, uidvalidity, updated_at) "
                "VALUES (?, ?, ?)",
                (mailbox_key, uidvalidity, _now()),
            )
        elif int(row["uidvalidity"]) != int(uidvalidity):
            raise UidvalidityChangedError(
                f"UIDVALIDITY 已变化: 数据库={row['uidvalidity']} "
                f"服务器={uidvalidity}，拒绝采集并保留旧数据"
            )


def get_completed_uids(conn: sqlite3.Connection, mailbox_key: str) -> set[int]:
    rows = conn.execute(
        "SELECT uid FROM collected_uids WHERE mailbox_key = ?",
        (mailbox_key,),
    ).fetchall()
    return {int(r["uid"]) for r in rows}


def commit_message(
    conn: sqlite3.Connection,
    mailbox_key: str,
    uid: int,
    uidvalidity: int,
    rows: Sequence[Dict[str, Any]],
) -> int:
    """原子提交一封邮件：先校验全部行，再同时写结果与进度。

    - 相同 result_id 且内容完全一致 -> 去重（库内已存在则跳过，
      封内重复则只保留一条）；
    - 相同 result_id 但任一内容字段不同 -> 抛 ContentConflictError，
      事务回滚，整封拒绝，进度不推进。

    返回实际新写入的结果行数。
    """
    # 封内先做一次同内容去重 / 异内容冲突判定，保证待插入集合自身一致。
    pending: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        existing = pending.get(row["result_id"])
        if existing is not None and not _same_content(existing, row):
            raise ContentConflictError(
                f"封内结果 ID {row['result_id']} 出现内容不同的重复行"
            )
        pending.setdefault(row["result_id"], row)

    conn.execute("BEGIN IMMEDIATE")
    try:
        saved = conn.execute(
            "SELECT uidvalidity FROM mailboxes WHERE mailbox_key = ?",
            (mailbox_key,),
        ).fetchone()
        if saved is None or int(saved["uidvalidity"]) != int(uidvalidity):
            raise UidvalidityChangedError(
                "提交时发现 UIDVALIDITY 不一致，拒绝写入"
            )

        already = conn.execute(
            "SELECT 1 FROM collected_uids WHERE mailbox_key = ? AND uid = ?",
            (mailbox_key, uid),
        ).fetchone()
        if already is not None:
            # 已提交邮件不重入：直接回滚（无写入），由调用方跳过。
            conn.execute("ROLLBACK")
            return 0

        inserted = 0
        for result_id, row in pending.items():
            db_row = conn.execute(
                "SELECT well_position, analyte, measured_at, measured_value "
                "FROM results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
            if db_row is not None:
                if not _same_content(
                    {
                        "well_position": db_row["well_position"],
                        "analyte": db_row["analyte"],
                        "measured_at": int(db_row["measured_at"]),
                        "measured_value": db_row["measured_value"],
                    },
                    row,
                ):
                    raise ContentConflictError(
                        f"结果 ID {result_id} 已存在但内容不同，整封拒绝"
                    )
                continue
            conn.execute(
                "INSERT INTO results "
                "(result_id, well_position, analyte, measured_at, measured_value) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result_id,
                    row["well_position"],
                    row["analyte"],
                    int(row["measured_at"]),
                    row["measured_value"],
                ),
            )
            inserted += 1

        conn.execute(
            "INSERT INTO collected_uids "
            "(mailbox_key, uid, uidvalidity, has_csv, row_count, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                mailbox_key,
                uid,
                uidvalidity,
                1 if rows else 0,
                len(rows),
                _now(),
            ),
        )
        conn.execute(
            "UPDATE mailboxes SET updated_at = ? WHERE mailbox_key = ?",
            (_now(), mailbox_key),
        )
        conn.execute("COMMIT")
        return inserted
    except Exception:
        _safe_rollback(conn)
        raise


def _same_content(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    keys = ("well_position", "analyte", "measured_at", "measured_value")
    return all(a[k] == b[k] for k in keys)


# --------------------------------------------------------------------------- #
# 查询与统计
# --------------------------------------------------------------------------- #


def query_results(
    conn: sqlite3.Connection, result_ids: Sequence[str]
) -> List[Dict[str, Any]]:
    """按结果 ID 批量查询，返回可直接用作 reconcile 的 result_rows 的数组。

    保持请求 ID 的去重后顺序；数据库中不存在的 ID 不包含在响应中。
    """
    if not result_ids:
        return []
    ordered_ids = list(dict.fromkeys(result_ids))
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"SELECT * FROM results WHERE result_id IN ({placeholders})",
        ordered_ids,
    ).fetchall()
    by_id = {r["result_id"]: r for r in rows}
    return [
        {
            "result_id": r["result_id"],
            "well_position": r["well_position"],
            "analyte": r["analyte"],
            "measured_at": int(r["measured_at"]),
            # 检测值在采集端按字符串保存，查询时原样返回字符串。
            "measured_value": r["measured_value"],
        }
        for rid in ordered_ids
        if (r := by_id.get(rid)) is not None
    ]


def mailbox_progress(
    conn: sqlite3.Connection, mailbox_key: str
) -> Dict[str, Any]:
    state = get_uidvalidity(conn, mailbox_key)
    agg = conn.execute(
        "SELECT COUNT(*) AS msg_count, "
        "COALESCE(SUM(row_count), 0) AS row_count, "
        "COALESCE(MAX(uid), 0) AS last_uid "
        "FROM collected_uids WHERE mailbox_key = ?",
        (mailbox_key,),
    ).fetchone()
    return {
        "mailbox": mailbox_key,
        "uidvalidity": state,
        "completed_messages": int(agg["msg_count"]),
        "stored_rows": int(agg["row_count"]),
        "last_completed_uid": int(agg["last_uid"]),
    }


def total_results(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM results").fetchone()["c"])
