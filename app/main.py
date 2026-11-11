"""FastAPI 应用：旧仪器结果核对 + IMAP 采集入库。"""

from __future__ import annotations

import os
import sqlite3
from typing import List

from fastapi import FastAPI, HTTPException

from . import db
from .collector import mailbox_key, run_collection
from .config import ImapSettings, database_path
from .imapclient import ImapConnectionError, ImapFetchError
from .matching import solve
from .schemas import (
    CollectResponse,
    CollectStatusResponse,
    ReconcileRequest,
    ReconcileResponse,
    RepresentativePair,
    ResultQueryRequest,
    ResultQueryResponse,
    ResultVerdictResponse,
    StoredResultRow,
)

app = FastAPI(
    title="环境检测实验室结果核对与 IMAP 采集 API",
    description=(
        "## 功能\n\n"
        "1. **IMAP 采集**：从仪器共享邮箱按 UID 递增收取 UTF-8 CSV 附件，"
        "整封校验通过后原子写入 SQLite，并记录 UIDVALIDITY 与已完成 UID；"
        "断线、坏行、取信失败、内容冲突均停在该封之前，重启可续采。\n\n"
        "2. **结果查询**：按最多 50 个结果 ID 返回已采集结果，"
        "数组可直接作为核对接口的 `result_rows`。\n\n"
        "3. **归属核对**：把缺少样品号的旧仪器结果按 "
        "孔位 + 检测项目 + 时间容差 一对一归入进样计划，"
        "优化目标（字典序）为先最大化配对数、再最小化绝对时间差总和。\n\n"
        "## 采集示例流程（本地 IMAP 演示源随 docker compose 启动）\n\n"
        "```\n"
        "POST /api/v1/collect/imap        # 触发采集；可重复调用，失败后自动续采\n"
        "GET  /api/v1/collect/status      # 查看 UIDVALIDITY、完成进度与锁状态\n"
        "POST /api/v1/results/query       # 按 result_ids 取回 result_rows\n"
        "POST /api/v1/reconcile           # 把 result_rows 与计划行核对\n"
        "```\n"
    ),
    version="2.0.0",
)


def _get_db() -> sqlite3.Connection:
    """每个请求一个 SQLite 连接；WAL 模式下多进程/多请求可并发读。"""
    conn = db.connect(database_path())
    db.init_db(conn)
    return conn


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# IMAP 采集
# --------------------------------------------------------------------------- #


@app.post(
    "/api/v1/collect/imap",
    tags=["IMAP 采集"],
    response_model=CollectResponse,
    summary="按环境配置连接 IMAP，增量采集一封封邮件的 CSV 结果（无请求体）",
    openapi_extra={
        "x-code-samples": [
            {
                "lang": "bash",
                "source": "curl -s -X POST http://localhost:8000/api/v1/collect/imap",
            }
        ],
    },
)
def collect_imap() -> CollectResponse:
    settings = ImapSettings.from_env()
    holder = f"pid={os.getpid()}"
    conn = _get_db()
    try:
        try:
            report = run_collection(conn, settings, holder)
        except (ImapConnectionError, ImapFetchError) as exc:
            # 必须在泛化 RuntimeError 之前：IMAP 异常本身也是 RuntimeError
            status = 502
            prefix = (
                "IMAP 连接失败"
                if isinstance(exc, ImapConnectionError)
                else "IMAP 取信失败，已停在该封之前"
            )
            raise HTTPException(
                status_code=status, detail=f"{prefix}，未推进任何进度: {exc}"
            ) from exc
        except RuntimeError as exc:
            # ImapSettings.validate() / 环境变量格式问题：服务端配置错误
            raise HTTPException(
                status_code=500,
                detail=f"IMAP 环境配置无效，未推进任何进度: {exc}",
            ) from exc
        except db.LockHeldError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "另一个采集进程正在运行（采集锁被持有），本次请求被拒绝；"
                    f"详情: {exc}"
                ),
            ) from exc
        except db.UidvalidityChangedError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "邮箱 UIDVALIDITY 与已保存值不一致，已拒绝采集并保留旧数据；"
                    f"详情: {exc}"
                ),
            ) from exc
        return CollectResponse(**report.to_dict())
    finally:
        conn.close()


@app.get(
    "/api/v1/collect/status",
    tags=["IMAP 采集"],
    response_model=CollectStatusResponse,
    summary="查询采集进度、UIDVALIDITY 与采集锁状态",
)
def collect_status() -> CollectStatusResponse:
    settings = ImapSettings.from_env()
    conn = _get_db()
    try:
        key = mailbox_key(settings) if (settings.host and settings.username) else None
        progress = db.mailbox_progress(conn, key) if key else {
            "mailbox": None,
            "uidvalidity": None,
            "completed_messages": 0,
            "stored_rows": 0,
            "last_completed_uid": 0,
        }
        lock = db.get_lock_state(conn)
        return CollectStatusResponse(
            mailbox=progress["mailbox"],
            uidvalidity=progress["uidvalidity"],
            completed_messages=progress["completed_messages"],
            stored_rows=progress["stored_rows"],
            last_completed_uid=progress["last_completed_uid"],
            total_results=db.total_results(conn),
            collection_running=lock is not None,
            lock_holder=lock["holder"] if lock else None,
            lock_heartbeat_at=lock["heartbeat_at"] if lock else None,
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 结果查询
# --------------------------------------------------------------------------- #


@app.post(
    "/api/v1/results/query",
    tags=["结果查询"],
    response_model=ResultQueryResponse,
    summary="按最多 50 个结果 ID 查询已采集结果（可直接用作 result_rows）",
    openapi_extra={
        "x-code-samples": [
            {
                "lang": "bash",
                "source": (
                    "curl -s -X POST http://localhost:8000/api/v1/results/query \\\n"
                    "  -H 'Content-Type: application/json' \\\n"
                    "  -d '{\"result_ids\": [\"R-0001\", \"R-0002\"]}'"
                ),
            }
        ],
    },
)
def query_results(request: ResultQueryRequest) -> ResultQueryResponse:
    conn = _get_db()
    try:
        ordered_ids = list(dict.fromkeys(request.result_ids))
        rows = db.query_results(conn, ordered_ids)
        found_ids = {row["result_id"] for row in rows}
        return ResultQueryResponse(
            requested_count=len(ordered_ids),
            found_count=len(rows),
            missing_result_ids=[rid for rid in ordered_ids if rid not in found_ids],
            result_rows=[StoredResultRow(**row) for row in rows],
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 归属核对（原有能力）
# --------------------------------------------------------------------------- #


@app.post(
    "/api/v1/reconcile",
    tags=["核对"],
    response_model=ReconcileResponse,
    summary="执行结果与进样计划的最优核对",
    openapi_extra={
        "x-code-samples": [
            {
                "lang": "bash",
                "source": (
                    "curl -s -X POST http://localhost:8000/api/v1/reconcile \\\n"
                    "  -H 'Content-Type: application/json' \\\n"
                    "  --data @demo/request.json"
                ),
            }
        ],
    },
)
def reconcile(request: ReconcileRequest) -> ReconcileResponse:
    plans = request.plan_rows
    results = request.result_rows

    reconciliation = solve(plans, results, request.tolerance_seconds)

    plan_by_index = {idx: row for idx, row in enumerate(plans)}

    verdict_responses: List[ResultVerdictResponse] = []
    ambiguous_count = 0
    always_matched_count = 0

    for idx, result in enumerate(results):
        verdict = reconciliation.verdicts[idx]
        possible_ids = [
            plan_by_index[p].row_id for p in verdict.eligible_plan_indices
        ]
        always_matched = not verdict.can_be_unmatched
        if always_matched:
            always_matched_count += 1

        unique_and_certain = always_matched and len(possible_ids) == 1
        if unique_and_certain:
            assigned_plan = plan_by_index[verdict.eligible_plan_indices[0]]
            sample_no = assigned_plan.sample_no  # 计划行本身也可能没有样品号
            assigned_row_id = assigned_plan.row_id
        else:
            sample_no = None
            assigned_row_id = None
            # 歧义：有候选但归属不唯一，或存在使其未匹配的最优方案。
            # 无任何候选（确定无法配对）的结果不属于歧义，只是无归属。
            if possible_ids and (len(possible_ids) > 1 or verdict.can_be_unmatched):
                ambiguous_count += 1

        verdict_responses.append(
            ResultVerdictResponse(
                result_id=result.result_id,
                well_position=result.well_position,
                analyte=result.analyte,
                measured_at=result.measured_at,
                measured_value=result.measured_value,
                possible_plan_row_ids=possible_ids,
                can_be_unmatched=verdict.can_be_unmatched,
                always_matched=always_matched,
                sample_no=sample_no,
                assigned_plan_row_id=assigned_row_id,
            )
        )

    representative = [
        RepresentativePair(
            result_id=results[r].result_id,
            plan_row_id=plans[p].row_id,
            time_diff_seconds=abs(
                results[r].measured_at - plans[p].planned_at
            ),
        )
        for r, p in reconciliation.representative_matching
    ]

    return ReconcileResponse(
        tolerance_seconds=request.tolerance_seconds,
        pair_count=reconciliation.pair_count,
        total_time_diff_seconds=reconciliation.total_time_diff_seconds,
        ambiguous_result_count=ambiguous_count,
        always_matched_count=always_matched_count,
        representative_matching=representative,
        results=verdict_responses,
    )
