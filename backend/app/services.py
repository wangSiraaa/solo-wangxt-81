"""场景归一化（单位层）与求解编排：输入 -> 核心模型 -> 逐时段水量账。"""
from __future__ import annotations

import uuid
from typing import Any

from .core.curve import CurvePoint, StorageCurve
from .core.diagnose import diagnose
from .core.missing import ImputeMethod, impute_inflow
from .core.optimizer import Demand, SolveError, SolveResult, StepInput, solve
from .core.units import Dimension, Quant, UnitError, canonical
from .schemas import ScenarioIn, SolveOptions, SolveOut


class ScenarioError(ValueError):
    def __init__(self, message: str, *, missing_inflow: list[int] | None = None,
                 missing_demand: dict[str, list[int]] | None = None):
        super().__init__(message)
        self.missing_inflow = missing_inflow or []
        self.missing_demand = missing_demand or {}


# ---------- 单位归一化 ----------

def _quant_to_volume(q, duration_s: float, field: str) -> float | None:
    """体积/流量二选一：流量自动乘以步长。None 表示缺测（上游决定插补还是报错）。"""
    if q is None or getattr(q, "value", None) is None:
        return None
    quant = q if isinstance(q, Quant) else Quant(value=q.value, unit=q.unit)
    if quant.value is None:
        return None
    dim = quant.dimension()
    if dim == Dimension.VOLUME:
        return quant.canonical()
    if dim == Dimension.FLOW:
        return quant.canonical() * duration_s
    raise UnitError(f"{field} 的单位 '{quant.unit}' 是 {dim.value}，应为体积或流量")


def build_curve(scn: ScenarioIn) -> StorageCurve:
    pts = []
    for p in scn.curve:
        z = canonical(p.elevation, expect=Dimension.LENGTH)
        v = canonical(p.storage, expect=Dimension.VOLUME)
        a = canonical(p.area, expect=Dimension.AREA) if p.area else None
        pts.append(CurvePoint(z, v, a))
    return StorageCurve(
        pts,
        dead_storage_m3=canonical(scn.dead_storage, expect=Dimension.VOLUME),
        max_storage_m3=canonical(scn.max_storage, expect=Dimension.VOLUME),
    )


def normalize(scn: ScenarioIn, opts: SolveOptions | None = None):
    """返回 (curve, StepInput, demands_kwargs, meta)。缺测按选项插补，否则抛 ScenarioError。"""
    opts = opts or SolveOptions()
    curve = build_curve(scn)
    n = len(scn.steps)

    duration_s = [canonical(st.duration, expect=Dimension.TIME) for st in scn.steps]

    # 来水缺测：None 与 value=None 都视为缺测，绝不当零
    inflow_raw: list[float | None] = []
    for st, dt in zip(scn.steps, duration_s):
        q = st.inflow
        inflow_raw.append(_quant_to_volume(
            None if q is None or q.value is None else q, dt, "inflow"))
    try:
        inflow_imp = impute_inflow(inflow_raw, ImputeMethod(opts.impute_inflow.value))
    except Exception as e:
        missing = [i for i, v in enumerate(inflow_raw) if v is None]
        raise ScenarioError(
            f"来水存在缺测且未选择插补：{missing}。缺测来水不能当作零，"
            f"请选择 linear/mean/zero 显式插补或补测。",
            missing_inflow=missing,
        ) from e

    depth: list[float] = []
    eco: list[float] = []
    for i, st in enumerate(scn.steps):
        if st.net_evap_depth is None:
            depth.append(0.0)  # 未提供蒸发=按 0 计（与缺测区分），账上仍列蒸发行
        else:
            depth.append(canonical(st.net_evap_depth, expect=Dimension.LENGTH))
        eco.append(_quant_to_volume(st.eco_flow, duration_s[i], "eco_flow") or 0.0)

    steps = StepInput(
        inflow_m3=inflow_imp.values_m3,
        net_evap_depth_m=depth,
        eco_flow_m3=eco,
        duration_s=sum(duration_s) / n if duration_s else 1.0,
    )

    # 需求缺测（同样不许静默当零）
    demands: list[Demand] = []
    imputed_demand: dict[str, list[int]] = {}
    for d in scn.demands:
        raw = [
            None if q is None or q.value is None
            else _quant_to_volume(q, duration_s[i], f"demand:{d.key}")
            for i, q in enumerate(d.amount)
        ]
        missing = [i for i, v in enumerate(raw) if v is None]
        if missing and opts.impute_demand.value == "none":
            raise ScenarioError(
                f"需求 '{d.key}' 在时段 {missing} 缺测；请显式插补或补齐。",
                missing_demand={d.key: missing},
            )
        imp = impute_inflow(raw, ImputeMethod(opts.impute_demand.value))
        if imp.imputed_indexes:
            imputed_demand[d.key] = imp.imputed_indexes
        weight = d.weight
        if opts.weight_override and d.key in opts.weight_override:
            weight = opts.weight_override[d.key]
        demands.append(Demand(
            key=d.key, name=d.name, weight=float(weight),
            amount_m3=imp.values_m3, min_fraction=d.min_fraction,
        ))

    initial = canonical(scn.initial_storage, expect=Dimension.VOLUME)
    final = (canonical(opts.final_storage, expect=Dimension.VOLUME)
             if opts and opts.final_storage else
             (canonical(scn.final_storage, expect=Dimension.VOLUME)
              if scn.final_storage else None))
    spill = None
    spill_src = opts.spillway_capacity if (opts and opts.spillway_capacity) else scn.spillway_capacity
    if spill_src:
        q = Quant(value=spill_src.value, unit=spill_src.unit)
        dim = q.dimension()
        # 单步最大弃水量；给的是流量时按一个步长换算（取第一步时长做说明）
        spill = q.canonical() * (duration_s[0] if dim == Dimension.FLOW else 1.0)

    meta = {
        "duration_s": duration_s,
        "labels": [st.label for st in scn.steps],
        "imputed_inflow": inflow_imp.imputed_indexes,
        "impute_method": inflow_imp.method.value,
        "imputed_demand": imputed_demand,
        "initial_storage": initial,
        "final_storage": final,
        "spillway": spill,
    }
    return curve, steps, demands, meta


# ---------- 求解与水量账 ----------

def _ledger(scn: ScenarioIn, curve: StorageCurve, meta: dict, res: SolveResult,
            *, prefix_len: int = 0, notice_steps: int = 0):
    rows = []
    keys = [d.key for d in scn.demands]
    n = len(scn.steps)
    commit = res.commitment_m3 or {}
    short = res.shortfall_m3 or {}
    for t in range(n):
        deliver = {k: res.delivery_m3[k][t] for k in keys}
        demand = {k: res.demand_m3[k][t] for k in keys}
        deficit = {k: res.deficit_m3[k][t] for k in keys}
        rows.append({
            "index": t,
            "label": meta["labels"][t],
            "duration_s": meta["duration_s"][t],
            "start_storage_m3": res.storage_m3[t],
            "inflow_m3": res.inflow_m3[t],
            "inflow_imputed": t in meta["imputed_inflow"],
            "evaporation_m3": res.evaporation_m3[t],
            "delivery_m3": deliver,
            "delivery_total_m3": sum(deliver.values()),
            "spill_m3": res.spill_m3[t],
            "end_storage_m3": res.storage_m3[t + 1],
            "end_elevation_m": curve.elevation(
                min(max(res.storage_m3[t + 1], curve.dead_storage_m3), curve.max_storage_m3)),
            "demand_m3": demand,
            "deficit_m3": deficit,
            "eco_requirement_m3": res.eco_requirement_m3[t],
            "eco_release_m3": res.eco_release_m3[t],
            "eco_ok": res.eco_release_m3[t] + 1e-5 >= res.eco_requirement_m3[t],
            "conservation_residual_m3": res.conservation_residual_m3[t],
            "fixed": t < prefix_len,
            "notice_locked": prefix_len <= t < prefix_len + notice_steps,
            "commitment_m3": {k: (commit.get(k) or [0.0] * n)[t] for k in keys},
            "shortfall_m3": {k: (short.get(k) or [0.0] * n)[t] for k in keys},
        })
    levels = [curve.elevation(min(max(s, curve.dead_storage_m3), curve.max_storage_m3))
              for s in res.storage_m3]
    return rows, levels


def run_scenario(scn: ScenarioIn, opts: SolveOptions | None = None, *,
                 scenario_id: str | None = None, scheme_id: str | None = None,
                 name: str | None = None) -> SolveOut:
    opts = opts or SolveOptions()
    curve, steps, demands, meta = normalize(scn, opts)

    common = dict(
        scenario_id=scenario_id,
        scheme_id=scheme_id,
        name=name or opts.name or "未命名方案",
        weights={d.key: d.weight for d in demands},
        imputed_inflow_indexes=meta["imputed_inflow"],
        imputed_demand_indexes=meta["imputed_demand"],
        impute_method=meta["impute_method"],
        storage_curve_points=curve,
        demand_names={d.key: d.name for d in demands},
    )

    try:
        res = solve(
            curve, steps, demands,
            initial_storage_m3=meta["initial_storage"],
            final_storage_m3=meta["final_storage"],
            spillway_capacity_m3=meta["spillway"],
            imputed_indexes=meta["imputed_inflow"],
            impute_method=meta["impute_method"],
        )
    except SolveError:
        # 无可行方案：逐时段诊断，指出冲突时段（仍返回账目供图表追踪）
        diag = diagnose(
            curve, steps.inflow_m3, steps.net_evap_depth_m, steps.eco_flow_m3,
            {d.key: d.amount_m3 for d in demands},
            weights={d.key: d.weight for d in demands},
            initial_storage_m3=meta["initial_storage"],
            spillway_capacity_m3=meta["spillway"],
            final_storage_m3=meta["final_storage"],
        )
        fake = SolveResult(
            status="infeasible",
            storage_m3=diag.storage_m3,
            inflow_m3=steps.inflow_m3,
            evaporation_m3=[0.0] * len(steps.inflow_m3),
            spill_m3=diag.spill_m3,
            delivery_m3=diag.delivery_m3,
            demand_m3={d.key: d.amount_m3 for d in demands},
            deficit_m3=diag.deficit_m3,
            eco_release_m3=[0.0] * len(steps.inflow_m3),
            eco_requirement_m3=steps.eco_flow_m3,
            weights={d.key: d.weight for d in demands},
            conservation_residual_m3=diag.forced_loss_m3,
            evap_iterations=0, objective=None,
        )
        ledger, levels = _ledger(scn, curve, meta, fake)
        return SolveOut(
            status="infeasible", feasible=False,
            weights=_norm_weights(common["weights"]),
            imputed_inflow_indexes=common["imputed_inflow_indexes"],
            imputed_demand_indexes=common["imputed_demand_indexes"],
            impute_method=common["impute_method"],
            evap_iterations=0, objective=None, totals={},
            storage_curve=_curve_dicts(curve), level_m=levels,
            ledger=ledger,
            conflicts=[{
                "index": c.index, "kind": c.kind, "message": c.message,
                "storage_balance_m3": c.storage_balance_m3,
                "storage_bound_m3": c.storage_bound_m3, "gap_m3": c.gap_m3,
            } for c in diag.conflicts],
            demand_names=common["demand_names"],
            name=common["name"], scenario_id=scenario_id, scheme_id=scheme_id,
        )

    ledger, levels = _ledger(scn, curve, meta, res)
    return SolveOut(
        status="optimal", feasible=True,
        weights=res.weights,
        imputed_inflow_indexes=res.imputed_indexes,
        imputed_demand_indexes=meta["imputed_demand"],
        impute_method=res.impute_method,
        evap_iterations=res.evap_iterations,
        objective=res.objective, totals=res.totals,
        storage_curve=_curve_dicts(curve),
        level_m=levels, ledger=ledger, conflicts=[],
        demand_names=common["demand_names"],
        name=common["name"], scenario_id=scenario_id, scheme_id=scheme_id,
    )


def _norm_weights(w: dict[str, float]) -> dict[str, float]:
    s = sum(w.values()) or 1.0
    return {k: v / s for k, v in w.items()}


def _curve_dicts(curve: StorageCurve) -> list[dict[str, float]]:
    return [{"elevation_m": p.elevation_m, "storage_m3": p.storage_m3,
             "area_m2": p.area_m2 or 0.0} for p in curve.as_points()]


def new_id() -> str:
    return uuid.uuid4().hex[:12]
