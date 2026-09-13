"""API 的 Pydantic 模型（输入带单位标签；输出统一为 m³ / m）。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ImputeMethodIn(str, Enum):
    NONE = "none"
    LINEAR = "linear"
    MEAN = "mean"
    ZERO = "zero"


class QuantIn(BaseModel):
    """带单位的标量输入。"""
    value: float | None = None
    unit: str


class CurvePointIn(BaseModel):
    elevation: QuantIn
    storage: QuantIn
    area: QuantIn | None = None


class DemandIn(BaseModel):
    key: str
    name: str
    weight: float = Field(ge=0, description="公开权重，相对值即可")
    amount: list[QuantIn | None] = Field(description="逐时段需求量（可缺测，需显式插补）")
    min_fraction: float = Field(0.0, ge=0, le=1, description="最低保证比例（硬下限）")


class StepIn(BaseModel):
    label: str | None = None
    duration: QuantIn = Field(description="步长，如 {value:30,unit:'d'}")
    inflow: QuantIn | None = Field(None, description="时段总入流（体积）或平均流量（流量）")
    net_evap_depth: QuantIn | None = Field(None, description="水面净蒸发水深（蒸发-降水）")
    eco_flow: QuantIn | None = Field(None, description="最低生态流量（流量）或总水量（体积）")


class ScenarioIn(BaseModel):
    name: str
    description: str = ""
    curve: list[CurvePointIn]
    dead_storage: QuantIn
    max_storage: QuantIn
    initial_storage: QuantIn
    final_storage: QuantIn | None = Field(None, description="期末目标库容（可选，硬约束）")
    spillway_capacity: QuantIn | None = Field(None, description="单步溢洪道最大水量（体积）或流量")
    steps: list[StepIn]
    demands: list[DemandIn]

    @model_validator(mode="after")
    def _checks(self) -> "ScenarioIn":
        if not self.steps:
            raise ValueError("至少需要一个时段")
        n = len(self.steps)
        keys = [d.key for d in self.demands]
        if len(set(keys)) != len(keys):
            raise ValueError("需求 key 不能重复")
        if not any(d.weight > 0 for d in self.demands):
            raise ValueError("至少一个需求权重为正（权重需公开）")
        for d in self.demands:
            if len(d.amount) != n:
                raise ValueError(f"需求 {d.key} 的序列长度({len(d.amount)})必须等于时段数({n})")
        return self


class SolveOptions(BaseModel):
    impute_inflow: ImputeMethodIn = ImputeMethodIn.NONE
    impute_demand: ImputeMethodIn = ImputeMethodIn.NONE
    # 方案对比时可临时覆盖权重（不改动场景）；{key: weight}
    weight_override: dict[str, float] | None = None
    final_storage: QuantIn | None = None
    spillway_capacity: QuantIn | None = None
    name: str | None = None


class ConflictOut(BaseModel):
    index: int
    kind: str
    message: str
    storage_balance_m3: float
    storage_bound_m3: float
    gap_m3: float


class LedgerRow(BaseModel):
    """逐时段水量账（一行讲清：水从哪来，到哪去）。"""
    index: int
    label: str | None
    duration_s: float
    start_storage_m3: float
    inflow_m3: float
    inflow_imputed: bool
    evaporation_m3: float
    delivery_m3: dict[str, float]
    delivery_total_m3: float
    spill_m3: float
    end_storage_m3: float
    end_elevation_m: float
    demand_m3: dict[str, float]
    deficit_m3: dict[str, float]
    eco_requirement_m3: float
    eco_release_m3: float
    eco_ok: bool
    conservation_residual_m3: float
    fixed: bool = False                  # 已实际执行，优化器无权改写
    notice_locked: bool = False          # 通知期内承诺，硬固定
    commitment_m3: dict[str, float] = {}
    shortfall_m3: dict[str, float] = {}


class SolveOut(BaseModel):
    status: str
    scenario_id: str | None = None
    scheme_id: str | None = None
    name: str
    feasible: bool
    weights: dict[str, float]
    imputed_inflow_indexes: list[int]
    imputed_demand_indexes: dict[str, list[int]]
    impute_method: str
    evap_iterations: int
    objective: float | None
    totals: dict[str, float]
    storage_curve: list[dict[str, float]]
    level_m: list[float]
    ledger: list[LedgerRow]
    conflicts: list[ConflictOut] = []
    demand_names: dict[str, str]


class ScenarioMeta(BaseModel):
    id: str
    name: str
    description: str
    builtin: bool


class ScenarioOut(ScenarioMeta):
    payload: dict[str, Any]


class CompareOut(BaseModel):
    scenario_id: str
    schemes: list[SolveOut]


class LockOut(BaseModel):
    scheme_id: str
    locked: bool
