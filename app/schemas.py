"""请求/响应模型与输入校验。"""

from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

MAX_ROWS = 50


class PlanRow(BaseModel):
    """进样计划中的一行。"""

    row_id: str = Field(..., min_length=1, description="计划行唯一行号")
    sample_no: Optional[str] = Field(
        None, description="样品号；旧记录可能缺失，缺失为 null 或空串"
    )
    well_position: str = Field(..., min_length=1, description="孔位，如 A01")
    analyte: str = Field(..., min_length=1, description="检测项目，如 铅Pb")
    planned_at: StrictInt = Field(..., description="计划时间，整数 UTC 秒")

    @field_validator("sample_no")
    @classmethod
    def _normalize_sample_no(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("well_position", "analyte", "row_id")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空字符串")
        return value


class ResultRow(BaseModel):
    """旧仪器返回的一条结果。"""

    result_id: str = Field(..., min_length=1, description="结果唯一号")
    well_position: str = Field(..., min_length=1, description="孔位，如 A01")
    analyte: str = Field(..., min_length=1, description="检测项目，如 铅Pb")
    measured_at: StrictInt = Field(..., description="实测时间，整数 UTC 秒")
    measured_value: Union[StrictInt, float, str, bool] = Field(
        ..., description="检测值；数字或字符串（如 未检出），响应中原样保留"
    )

    @field_validator("well_position", "analyte", "result_id")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空字符串")
        return value


class ReconcileRequest(BaseModel):
    tolerance_seconds: StrictInt = Field(
        ..., ge=0, description="允许的最大绝对时间差（含边界），整数 UTC 秒"
    )
    plan_rows: List[PlanRow] = Field(..., max_length=MAX_ROWS)
    result_rows: List[ResultRow] = Field(..., max_length=MAX_ROWS)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "ReconcileRequest":
        plan_ids = [row.row_id for row in self.plan_rows]
        if len(set(plan_ids)) != len(plan_ids):
            raise ValueError("plan_rows.row_id 必须唯一")
        result_ids = [row.result_id for row in self.result_rows]
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("result_rows.result_id 必须唯一")
        return self


class ResultVerdictResponse(BaseModel):
    result_id: str = Field(..., description="结果唯一号，与输入对应")
    well_position: str
    analyte: str
    measured_at: StrictInt
    measured_value: Union[StrictInt, float, str, bool] = Field(
        ..., description="检测值原样保留"
    )
    possible_plan_row_ids: List[str] = Field(
        ...,
        description="在全部最优方案中该结果可能归属的计划行号；无任何可能配对时为空数组",
    )
    can_be_unmatched: bool = Field(
        ..., description="是否存在某个最优方案使该结果未匹配"
    )
    always_matched: bool = Field(
        ..., description="是否在全部最优方案中都处于匹配状态"
    )
    sample_no: Optional[str] = Field(
        ...,
        description=(
            "样品号；仅当归属始终唯一且始终匹配时填入，"
            "歧义或可能未匹配时为 null，避免误导入"
        ),
    )
    assigned_plan_row_id: Optional[str] = Field(
        ..., description="与 sample_no 同一条件下填入的计划行号，否则为 null"
    )


class RepresentativePair(BaseModel):
    result_id: str
    plan_row_id: str
    time_diff_seconds: StrictInt


class ReconcileResponse(BaseModel):
    tolerance_seconds: StrictInt
    pair_count: int = Field(..., description="最优方案的配对数（最大配对数）")
    total_time_diff_seconds: int = Field(
        ..., description="最优方案的时间差总和（最大配对数下的最小值）"
    )
    ambiguous_result_count: int = Field(
        ...,
        description=(
            "歧义结果数：存在多个可能归属，或存在使其未匹配的最优方案；"
            "确定无候选可配的结果不计入"
        ),
    )
    always_matched_count: int = Field(..., description="在所有最优方案中都匹配的结果数")
    representative_matching: List[RepresentativePair] = Field(
        ..., description="一个代表性最优方案，仅供参考，不应直接作为确定归属导入"
    )
    results: List[ResultVerdictResponse] = Field(
        ..., description="逐条结果的核对结论，顺序与输入一致"
    )
