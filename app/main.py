"""FastAPI 应用：旧仪器结果与进样计划核对。"""

from __future__ import annotations

from typing import List

from fastapi import FastAPI

from .matching import solve
from .schemas import (
    ReconcileRequest,
    ReconcileResponse,
    RepresentativePair,
    ResultVerdictResponse,
)

app = FastAPI(
    title="环境检测实验室结果核对 API",
    description=(
        "把缺少样品号的旧仪器结果按 孔位 + 检测项目 + 时间容差 一对一归入进样计划。\n\n"
        "优化目标（字典序）：先最大化配对数，再最小化绝对时间差总和。\n\n"
        "对每条结果返回其在 **全部最优方案** 中可能归属的计划行号，"
        "以及是否可能未匹配；仅当归属始终唯一且始终匹配时才回填样品号，"
        "检测值始终原样保留，避免歧义结果误导入 LIMS。"
    ),
    version="1.0.0",
)


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/reconcile",
    tags=["核对"],
    response_model=ReconcileResponse,
    summary="执行结果与进样计划的最优核对",
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
