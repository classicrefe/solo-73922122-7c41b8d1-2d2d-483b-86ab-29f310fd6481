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

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "tolerance_seconds": 60,
                    "plan_rows": [
                        {"row_id": "P1", "sample_no": "S-001",
                         "well_position": "A01", "analyte": "铅Pb",
                         "planned_at": 1000}
                    ],
                    "result_rows": [
                        # 采集接口返回的 result_rows 可直接粘贴到这里
                        {"result_id": "R-0001", "well_position": "A01",
                         "analyte": "铅Pb", "measured_at": 1000,
                         "measured_value": "0.12"}
                    ],
                }
            ]
        }
    }

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


# --------------------------------------------------------------------------- #
# IMAP 采集与结果查询
# --------------------------------------------------------------------------- #


class ResultQueryRequest(BaseModel):
    """按结果 ID 批量查询已采集结果。"""

    result_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_ROWS,
        description="待查询结果 ID 列表，最多 50 个；重复 ID 自动去重",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"result_ids": ["R-0001", "R-0002", "R-0003"]}]
        }
    }

    @field_validator("result_ids")
    @classmethod
    def _strip_and_reject_blank(cls, values: List[str]) -> List[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("result_ids 不能包含空字符串")
        return cleaned


class StoredResultRow(BaseModel):
    """SQLite 中保存的仪器结果，字段与 ResultRow 兼容，可直接用于核对接口。"""

    result_id: str
    well_position: str
    analyte: str
    measured_at: StrictInt = Field(..., description="实测时间，整数 UTC 秒")
    measured_value: str = Field(
        ..., description="检测值；采集端始终按字符串原样保留"
    )


class ResultQueryResponse(BaseModel):
    requested_count: int = Field(..., description="请求中去重后的结果 ID 数")
    found_count: int = Field(..., description="数据库中实际命中的结果数")
    missing_result_ids: List[str] = Field(
        ..., description="未找到的结果 ID，按请求顺序排列"
    )
    result_rows: List[StoredResultRow] = Field(
        ..., description="命中的结果数组，字段结构与核对接口 result_rows 一致"
    )


class CollectResponse(BaseModel):
    status: str = Field(
        ...,
        description=(
            "completed=本轮无待处理或待处理邮件全部成功；"
            "stopped=在 failed_uid 处停止，未推进该封进度，重启/再次调用可续采；"
            "uidvalidity_rejected=邮箱 UIDVALIDITY 变化，拒绝采集且保留旧数据"
        ),
    )
    mailbox: str = Field(..., description="邮箱标识（用户名@主机/文件夹）")
    uidvalidity: Optional[StrictInt] = Field(
        ..., description="本次连接读到的邮箱 UIDVALIDITY"
    )
    processed_uids: List[StrictInt] = Field(
        ..., description="本次调用成功提交（含无 CSV 记完成）的邮件 UID，递增"
    )
    processed_count: int
    inserted_rows: int = Field(..., description="本次新写入 results 表的行数")
    duplicate_rows: int = Field(
        ..., description="同结果 ID 同内容去重而跳过的行数"
    )
    messages_without_csv: int = Field(
        ..., description="无 CSV 附件、按完成记录的邮件数"
    )
    failed_uid: Optional[StrictInt] = Field(
        ..., description="停止位置的邮件 UID；成功完成时为 null"
    )
    reason: Optional[str] = Field(..., description="停止或拒绝原因")
    remaining_pending_uids: List[StrictInt] = Field(
        ..., description="尚未处理的待办 UID（停止位置起，递增）"
    )


class CollectStatusResponse(BaseModel):
    mailbox: Optional[str]
    uidvalidity: Optional[StrictInt]
    completed_messages: int = Field(..., description="已完成邮件数（含无 CSV 邮件）")
    stored_rows: int = Field(..., description="results 表累计保存行数")
    last_completed_uid: StrictInt
    total_results: int = Field(..., description="全库结果总数")
    collection_running: bool = Field(..., description="当前是否有采集持锁运行")
    lock_holder: Optional[str] = None
    lock_heartbeat_at: Optional[StrictInt] = None
