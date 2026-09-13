"""滚动计划相关 API 模型。

时间编号约定：所有 step_index 均为"场景绝对时段编号"（从 0 开始）。
- 已实际执行的前缀 actual：0 .. m-1（不可被优化改写）；
- 新预测 forecast：自 m 起的未来时段；
- 已确认计划 confirmed：过去某次确认时保存的完整快照，承诺 C 取自其中
  m 起的逐户供水量；通知期 notice_steps 内的承诺为硬等式。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .schemas import QuantIn, SolveOut


class ActualStepIn(BaseModel):
    step_index: int = Field(ge=0)
    label: str | None = None
    duration: QuantIn
    inflow: QuantIn = Field(description="实际入流（体积或平均流量）；缺测直接报错，不用预测顶替")
    net_evap_depth: QuantIn | None = None
    eco_flow: QuantIn | None = None
    delivery: dict[str, QuantIn] = Field(default_factory=dict, description="实际逐户供水量")
    spill: QuantIn | None = None
    measured_storage: QuantIn | None = Field(
        None, description="实测当前/期末库容；给了就以实测为准重算，余额记为账面调整")


class ForecastStepIn(BaseModel):
    step_index: int = Field(ge=0)
    label: str | None = None
    duration: QuantIn
    inflow: QuantIn | None = Field(None, description="预测入流；缺测需显式插补")
    net_evap_depth: QuantIn | None = None
    eco_flow: QuantIn | None = None


class ScenarioForecastIn(BaseModel):
    """一个来水情景：乐观/基准/悲观。probability 给了才参与概率输出。"""
    key: str
    name: str
    probability: float | None = Field(None, ge=0, le=1)
    steps: list[ForecastStepIn]


class CurveRevisionIn(BaseModel):
    points: list[dict[str, Any]] = Field(
        description="修订后的完整库容曲线（elevation/storage[/area] 带单位）")
    dead_storage: QuantIn
    max_storage: QuantIn
    convert_initial: bool = Field(
        True, description="按时段末水位把真实当前库容换算到新曲线（默认开启）")


class ContractIn(BaseModel):
    """合同示例规则（教学参数，非真实合同文本）。"""
    notice_steps: int = Field(2, ge=0, description="通知期：起算自当前时刻，期内承诺硬固定")
    down_cost_multiplier: float = Field(
        10.0, gt=0, description="欠诺默认单价 = 倍数 × 公开权重（每 m³）")
    emergency: bool = Field(False, description="紧急豁免：允许击穿最低保证比例，代价照计")
    emergency_note: str = ""


class ReplanRequest(BaseModel):
    scenario: dict[str, Any] = Field(description="ScenarioIn（用户可能已编辑基础参数）")
    forecast_version: str
    forecast: list[ForecastStepIn]
    actual: list[ActualStepIn] = []
    confirmed: dict[str, Any] | None = Field(
        None, description="已确认计划快照（含 forecast_version/result/contract）")
    contract: ContractIn = ContractIn()
    impute_inflow: str = "none"
    curve_revision: CurveRevisionIn | None = None
    evaluated_scenarios: list[ScenarioForecastIn] | None = Field(
        None, description="若提供：同一方案在多来水情景下的缺口区间，一并返回")


class ReconciliationOut(BaseModel):
    """账面调整（执行偏差/曲线修订造成的一次性库容差）。"""
    period: str
    book_storage_m3: float
    measured_storage_m3: float
    adjustment_m3: float
    reason: str


class AttributionOut(BaseModel):
    """新计划相对已确认计划的逐户、总量差异分解（m³，未来视界内）。"""
    total_change_m3: dict[str, float]
    due_to_forecast_m3: dict[str, float]
    due_to_execution_m3: dict[str, float]
    due_to_curve_m3: dict[str, float]
    unexplained_m3: dict[str, float]
    current_storage_gap_m3: float
    current_storage_gap_due_to_execution_m3: float
    current_storage_gap_due_to_revaluation_m3: float
    actual_delivery_planned_m3: dict[str, list[float]]
    actual_delivery_m3: dict[str, list[float]]
    actual_execution_gap_m3: dict[str, float]
    actual_inflow_planned_m3: list[float]
    actual_inflow_m3: list[float]


class ScenarioRangeOut(BaseModel):
    key: str
    name: str
    probability: float | None
    feasible: bool
    deficit_total_m3: dict[str, float]
    deficit_range_per_period_m3: dict[str, list[float]]
    shortfall_total_m3: dict[str, float]
    change_cost_total: float
    final_storage_m3: float
    conflicts_count: int


class ReplanOut(BaseModel):
    plan: SolveOut                         # 主情景重算结果（含已执行前缀）
    forecast_version: str
    based_on_confirmed_version: str | None
    prefix_len: int
    horizon_len: int
    contract: dict[str, Any]
    actual_ledger: list[dict[str, Any]]
    commitments_m3: dict[str, list[float]]
    reconciliations: list[ReconciliationOut]
    attribution: AttributionOut
    ranges: list[ScenarioRangeOut] = []
    probability_note: str = ""
    curve_revision_applied: bool
    stale_actual_warning: str = ""


class ConfirmRequest(BaseModel):
    scenario_id: str
    forecast_version: str
    plan: dict[str, Any] = Field(description="本次重算返回的 plan（SolveOut）")
    actual_used_count: int = Field(description="确认时使用的实际流量条数")
    contract: dict[str, Any] = {}


class ConfirmOut(BaseModel):
    confirmation_id: str
    scenario_id: str
    forecast_version: str
    actual_used_count: int
    newer_actual_available: bool
    warning: str = ""
