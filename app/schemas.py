"""请求/响应模型。

年份用整数：公元后为正整数，公元前用负整数（如 -221 表示公元前 221 年）。
年代段为整年闭区间 [year_from, year_to]，一个层位可给多个区间，区间之间允许空档。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class IntervalIn(BaseModel):
    year_from: int = Field(..., description="区间起始年（含），公元前用负数")
    year_to: int = Field(..., description="区间结束年（含），公元前用负数")

    @field_validator("year_to")
    @classmethod
    def _check_order(cls, value: int, info) -> int:
        start = info.data.get("year_from")
        if start is not None and start > value:
            raise ValueError("year_from 不得晚于 year_to（闭区间整年）")
        return value


class SiteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="遗址名称")


class SiteOut(BaseModel):
    id: int
    name: str
    created: str


class StratumCreate(BaseModel):
    trench: str = Field(..., min_length=1, max_length=60, description="探方编号")
    layer_no: str = Field(..., min_length=1, max_length=60, description="层位编号（探方内唯一）")
    intervals: list[IntervalIn] = Field(
        ..., min_length=1, description="可能的沉积年代段，可多段、可有空档"
    )
    note: Optional[str] = Field(None, max_length=400)


class StratumOut(BaseModel):
    id: int
    site_id: int
    trench: str
    layer_no: str
    note: Optional[str]
    intervals: list[IntervalIn]
    created: str


class CorrelationCreate(BaseModel):
    stratum_a_id: int = Field(..., description="待对接层位 A")
    stratum_b_id: int = Field(..., description="待对接层位 B（须与 A 不同探方）")


class CorrelationOut(BaseModel):
    id: int
    site_id: int
    stratum_a_id: int
    stratum_b_id: int
    created: str


class SuperpositionCreate(BaseModel):
    lower_id: int = Field(..., description="下层层位 id")
    upper_id: int = Field(..., description="上层层位 id（不得与下层相同）")


class SuperpositionOut(BaseModel):
    id: int
    site_id: int
    lower_id: int
    upper_id: int
    created: str


class WitnessOut(BaseModel):
    kind: str
    record_id: int
    detail: str


class ContradictionOut(BaseModel):
    reason: str = Field(..., description="EMPTY_DOMAIN | GROUP_CYCLE | ORDER_CONFLICT")
    message: str
    witness: list[WitnessOut]


class GroupEdgeOut(BaseModel):
    from_group: int
    to_group: int
    edge_ids: list[int]


class GroupOut(BaseModel):
    group_id: int
    member_ids: list[int]
    domain: list[list[int]] = Field(..., description="组内共用的原始可能年代域（交集，含空档）")
    feasible_years: list[list[int]] = Field(
        ..., description="与所有其他层位约束可同时成立的全部年代段（含空档）"
    )
    earliest_year: Optional[int]
    latest_year: Optional[int]


class SolutionOut(BaseModel):
    feasible: bool
    version: int = Field(..., description="当前已完成提交的序号，每次成功写入 +1")
    groups: list[GroupOut]
    group_edges: list[GroupEdgeOut]
    group_of: dict[str, int]
    contradiction: Optional[ContradictionOut] = None
    earliest_assignment: Optional[dict[str, int]] = None
    latest_assignment: Optional[dict[str, int]] = None


class WriteResult(BaseModel):
    """所有写接口统一返回：操作对象 + 写入后的整站求解结果。"""

    record: dict
    solution: SolutionOut
