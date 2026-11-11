"""采集与查询接口的 FastAPI 集成测试。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tools import make_fixtures
from tools.imap_server import MessageStore, build_server

from .test_collect_imap import _free_port, _wait_port  # 复用测试工具


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "collect.db"
    monkeypatch.setenv("COLLECT_DB_PATH", str(db_file))
    mail_dir = tmp_path / "mailbox"
    make_fixtures.write_fixtures(str(mail_dir))

    port = _free_port()
    store = MessageStore(str(mail_dir), uidvalidity=1700000000)
    server = build_server("127.0.0.1", port, store, "lab", "labpass", "INBOX")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_port(port)

    monkeypatch.setenv("IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("IMAP_PORT", str(port))
    monkeypatch.setenv("IMAP_USERNAME", "lab")
    monkeypatch.setenv("IMAP_PASSWORD", "labpass")
    monkeypatch.setenv("IMAP_MAILBOX", "INBOX")
    monkeypatch.setenv("IMAP_USE_SSL", "false")
    monkeypatch.setenv("IMAP_CONNECT_TIMEOUT", "10")

    with TestClient(app) as test_client:
        yield test_client, db_file, mail_dir

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_collect_end_to_end_and_status(client):
    test_client, _db_file, _mail_dir = client
    resp = test_client.post("/api/v1/collect/imap")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "stopped"  # 夹具 UID4 是坏行
    assert data["processed_uids"] == [1, 2, 3]
    assert data["failed_uid"] == 4
    assert data["messages_without_csv"] == 1
    assert data["inserted_rows"] == 6
    assert data["uidvalidity"] == 1700000000

    status = test_client.get("/api/v1/collect/status").json()
    assert status["completed_messages"] == 3
    assert status["total_results"] == 6
    assert status["collection_running"] is False
    assert status["lock_holder"] is None


def test_query_endpoint_returns_result_rows_shape(client):
    test_client, _db_file, _mail_dir = client
    test_client.post("/api/v1/collect/imap")

    resp = test_client.post(
        "/api/v1/results/query",
        json={"result_ids": ["R-0004", "R-0001", "MISSING", "R-0004"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_count"] == 3  # 重复 ID 去重
    assert data["found_count"] == 2
    assert data["missing_result_ids"] == ["MISSING"]
    rows = data["result_rows"]
    assert [r["result_id"] for r in rows] == ["R-0004", "R-0001"]
    # 可直接喂给核对接口的字段结构
    assert set(rows[0]) == {
        "result_id", "well_position", "analyte", "measured_at", "measured_value",
    }
    assert isinstance(rows[0]["measured_at"], int)
    assert isinstance(rows[0]["measured_value"], str)


def test_query_limit_50_enforced(client):
    test_client, _db_file, _mail_dir = client
    resp = test_client.post(
        "/api/v1/results/query",
        json={"result_ids": [f"R{i}" for i in range(51)]},
    )
    assert resp.status_code == 422

    ok = test_client.post(
        "/api/v1/results/query",
        json={"result_ids": [f"R{i}" for i in range(50)]},
    )
    assert ok.status_code == 200


def test_query_empty_list_rejected(client):
    test_client, _db_file, _mail_dir = client
    resp = test_client.post("/api/v1/results/query", json={"result_ids": []})
    assert resp.status_code == 422


def test_query_results_can_drive_reconcile(client):
    """完整链路：采集 -> 查询 result_rows -> 原核对接口消费。"""
    test_client, _db_file, _mail_dir = client
    test_client.post("/api/v1/collect/imap")
    queried = test_client.post(
        "/api/v1/results/query",
        json={"result_ids": ["R-0001", "R-0002"]},
    ).json()["result_rows"]

    # 库里 measured_value 是字符串 "0.12"，核对接口按字符串原样接受
    plan_rows = [
        {"row_id": "P1", "sample_no": "S-001", "well_position": "A01",
         "analyte": "铅Pb", "planned_at": queried[1]["measured_at"]},
        {"row_id": "P2", "sample_no": "S-002", "well_position": "A01",
         "analyte": "铅Pb", "planned_at": queried[0]["measured_at"]},
    ]
    resp = test_client.post(
        "/api/v1/reconcile",
        json={
            "tolerance_seconds": 60,
            "plan_rows": plan_rows,
            "result_rows": queried,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pair_count"] == 2
    by_id = {r["result_id"]: r for r in data["results"]}
    assert by_id["R-0001"]["sample_no"] == "S-002"
    assert by_id["R-0002"]["sample_no"] == "S-001"
    # 字符串检测值原样保留
    assert by_id["R-0001"]["measured_value"] == "0.12"


def test_collect_lock_conflict_returns_409(client, monkeypatch):
    test_client, db_file, _mail_dir = client
    conn = db.connect(str(db_file))
    db.init_db(conn)
    token = db.acquire_lock(conn, "manual-holder", stale_seconds=300)
    try:
        resp = test_client.post("/api/v1/collect/imap")
        assert resp.status_code == 409
        assert "采集锁" in resp.json()["detail"]
    finally:
        db.release_lock(conn, token)
        conn.close()


def test_missing_imap_env_returns_500(client, monkeypatch):
    test_client, _db_file, _mail_dir = client
    monkeypatch.delenv("IMAP_HOST")
    resp = test_client.post("/api/v1/collect/imap")
    # 配置缺失属于服务端环境错误
    assert resp.status_code == 500


def test_bad_imap_host_returns_502(client, monkeypatch):
    test_client, _db_file, _mail_dir = client
    monkeypatch.setenv("IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("IMAP_PORT", "1")  # 拒连端口
    resp = test_client.post("/api/v1/collect/imap")
    assert resp.status_code == 502


def test_openapi_documents_new_endpoints(client):
    test_client, _db_file, _mail_dir = client
    spec = test_client.get("/openapi.json").json()
    for path in (
        "/api/v1/collect/imap",
        "/api/v1/collect/status",
        "/api/v1/results/query",
        "/api/v1/reconcile",
    ):
        assert path in spec["paths"]
