"""FastAPI 接口集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

DEMO = json.loads((Path(__file__).parent.parent / "demo" / "request.json").read_text())


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_document_available():
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert "/api/v1/reconcile" in spec["paths"]
    assert "docs"  # Swagger UI 由 FastAPI 默认挂载


def test_demo_request_end_to_end():
    resp = client.post("/api/v1/reconcile", json=DEMO)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    by_id = {r["result_id"]: r for r in data["results"]}

    # 1) 唯一最优归属 -> 回填样品号
    assert by_id["R1"]["sample_no"] == "S-001"
    assert by_id["R1"]["assigned_plan_row_id"] == "P1"
    assert by_id["R1"]["possible_plan_row_ids"] == ["P1"]
    assert by_id["R1"]["can_be_unmatched"] is False
    assert by_id["R1"]["always_matched"] is True
    assert by_id["R1"]["measured_value"] == 0.12

    assert by_id["R2"]["sample_no"] == "S-002"
    assert by_id["R2"]["assigned_plan_row_id"] == "P2"

    # 2) 完全等价的互换歧义 -> 两个可能归属，不回填样品号，但始终会匹配
    for rid in ("R3", "R4"):
        assert by_id[rid]["possible_plan_row_ids"] == ["P3", "P4"]
        assert by_id[rid]["can_be_unmatched"] is False
        assert by_id[rid]["always_matched"] is True
        assert by_id[rid]["sample_no"] is None
        assert by_id[rid]["assigned_plan_row_id"] is None

    # 3) 孔位/项目不符 -> 无归属、可未匹配
    assert by_id["R5"]["possible_plan_row_ids"] == []
    assert by_id["R5"]["can_be_unmatched"] is True
    assert by_id["R5"]["sample_no"] is None
    assert by_id["R5"]["measured_value"] == "未检出"  # 字符串检测值原样保留

    # 4) 一个计划行两个候选结果：费用 50 的 R6 始终匹配，费用 60 的 R7
    #    换入后总费用上升，不是最优 => R7 在所有最优方案中都未匹配
    assert by_id["R6"]["possible_plan_row_ids"] == ["P6"]
    assert by_id["R6"]["can_be_unmatched"] is False
    assert by_id["R6"]["sample_no"] == "S-006"
    assert by_id["R7"]["possible_plan_row_ids"] == []
    assert by_id["R7"]["can_be_unmatched"] is True

    # 汇总：P1/P2 两对 + B02 两对 + D04 一对 = 5；总时间差 50
    # 始终匹配 5 条（R1-R4、R6）；歧义 2 条（R3/R4 归属可互换）；
    # R5 无候选、R7 换入会抬高费用，均为确定未匹配，不计入歧义。
    assert data["pair_count"] == 5
    assert data["total_time_diff_seconds"] == 50
    assert data["always_matched_count"] == 5
    assert data["ambiguous_result_count"] == 2
    assert len(data["representative_matching"]) == 5


def test_empty_lists_allowed():
    resp = client.post(
        "/api/v1/reconcile",
        json={"tolerance_seconds": 0, "plan_rows": [], "result_rows": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pair_count"] == 0
    assert data["results"] == []


def test_blank_sample_no_normalized_to_null():
    payload = {
        "tolerance_seconds": 10,
        "plan_rows": [
            {"row_id": "P1", "sample_no": "   ", "well_position": "A01",
             "analyte": "Pb", "planned_at": 1000}
        ],
        "result_rows": [
            {"result_id": "R1", "well_position": "A01", "analyte": "Pb",
             "measured_at": 1000, "measured_value": 1}
        ],
    }
    data = client.post("/api/v1/reconcile", json=payload).json()
    # 归属确定，但计划行本身没有样品号 -> 仍是 null
    assert data["results"][0]["assigned_plan_row_id"] == "P1"
    assert data["results"][0]["sample_no"] is None


def test_duplicate_plan_row_id_rejected():
    payload = {
        "tolerance_seconds": 10,
        "plan_rows": [
            {"row_id": "P1", "well_position": "A01", "analyte": "Pb", "planned_at": 1},
            {"row_id": "P1", "well_position": "A02", "analyte": "Pb", "planned_at": 2},
        ],
        "result_rows": [],
    }
    assert client.post("/api/v1/reconcile", json=payload).status_code == 422


def test_duplicate_result_id_rejected():
    payload = {
        "tolerance_seconds": 10,
        "plan_rows": [],
        "result_rows": [
            {"result_id": "R1", "well_position": "A01", "analyte": "Pb",
             "measured_at": 1, "measured_value": 1},
            {"result_id": "R1", "well_position": "A02", "analyte": "Pb",
             "measured_at": 2, "measured_value": 2},
        ],
    }
    assert client.post("/api/v1/reconcile", json=payload).status_code == 422


def test_negative_tolerance_rejected():
    payload = {
        "tolerance_seconds": -1,
        "plan_rows": [],
        "result_rows": [],
    }
    assert client.post("/api/v1/reconcile", json=payload).status_code == 422


def test_boolean_time_rejected_by_strict_int():
    payload = {
        "tolerance_seconds": 10,
        "plan_rows": [
            {"row_id": "P1", "well_position": "A01", "analyte": "Pb",
             "planned_at": True}
        ],
        "result_rows": [],
    }
    assert client.post("/api/v1/reconcile", json=payload).status_code == 422


def test_more_than_50_rows_rejected():
    plans = [
        {"row_id": f"P{i}", "well_position": "A01", "analyte": "Pb",
         "planned_at": i}
        for i in range(51)
    ]
    payload = {"tolerance_seconds": 10, "plan_rows": plans, "result_rows": []}
    assert client.post("/api/v1/reconcile", json=payload).status_code == 422


def test_missing_required_field_rejected():
    payload = {
        "tolerance_seconds": 10,
        "plan_rows": [
            {"row_id": "P1", "analyte": "Pb", "planned_at": 1}  # 缺 well_position
        ],
        "result_rows": [],
    }
    assert client.post("/api/v1/reconcile", json=payload).status_code == 422
