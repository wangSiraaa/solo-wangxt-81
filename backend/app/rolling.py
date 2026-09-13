"""滚动计划服务：实际执行前缀固定 + 承诺合同代价 + 预测分版本 + 归因分解。

不改变历史原则：
  - 已执行时段的入流/供水/弃水来自 actual 输入，优化器只在未来时段决策；
  - 实测库容与账面库容不一致时，不回改历史账目，而是在重算起点记一条
    "账面调整(reconciliation)"，未来计划从真实当前库容开始；
  - 库容曲线修订同样在起点做一次性重估价（水位守恒，不是水量守恒）。

归因（让学员看清"计划为什么变了"）：用三组反事实 LP 把未来供水变化量分解为
  预测改变 / 执行偏差 / 曲线修订 / 未解释残差：
  P0 已确认的旧计划（旧预测+承诺起点）
  A  仅把起点换成执行后的真实库容（执行偏差）
  B  在 A 上换用新预测（+预测改变）
  D  完整新计划（+曲线修订与其他）
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from .core.curve import CurvePoint, StorageCurve
from .core.diagnose import diagnose
from .core.missing import ImputeMethod, impute_inflow
from .core.optimizer import Demand, Horizon, SolveError, SolveResult, StepInput, solve_horizon
from .core.units import Dimension, Quant, UnitError, canonical
from .rolling_schemas import (AttributionOut, ReconciliationOut, ScenarioRangeOut)
from .schemas import ScenarioIn, SolveOptions, SolveOut
from .services import ScenarioError, _curve_dicts, _ledger, _norm_weights, build_curve


# ---------- 输入归一化 ----------

def _to_volume(q, duration_s: float, field: str) -> float:
    if q is None:
        raise ScenarioError(f"{field}：实际数据缺测，不能用预测顶替或静默当零")
    if getattr(q, "value", None) is None:
        raise ScenarioError(f"{field}：实际数据缺测（value=null），请补测后再滚动")
    quant = q if isinstance(q, Quant) else Quant(value=q.value, unit=q.unit)
    dim = quant.dimension()
    if dim == Dimension.VOLUME:
        return quant.canonical()
    if dim == Dimension.FLOW:
        return quant.canonical() * duration_s
    raise UnitError(f"{field} 单位 '{quant.unit}' 应为体积或流量")


def _q(q, duration_s: float) -> float | None:
    if q is None or getattr(q, "value", None) is None:
        return None
    quant = q if isinstance(q, Quant) else Quant(value=q.value, unit=q.unit)
    return (quant.canonical() if quant.dimension() == Dimension.VOLUME
            else quant.canonical() * duration_s)


def _curve_from_revision(rev) -> StorageCurve:
    pts = []
    for p in rev.points:
        el = Quant(value=p["elevation"]["value"], unit=p["elevation"]["unit"])
        st = Quant(value=p["storage"]["value"], unit=p["storage"]["unit"])
        ar = None
        if p.get("area"):
            ar = Quant(value=p["area"]["value"], unit=p["area"]["unit"])
        pts.append(CurvePoint(
            canonical(el, expect=Dimension.LENGTH),
            canonical(st, expect=Dimension.VOLUME),
            canonical(ar, expect=Dimension.AREA) if ar else None,
        ))
    return StorageCurve(
        pts,
        dead_storage_m3=canonical(rev.dead_storage, expect=Dimension.VOLUME),
        max_storage_m3=canonical(rev.max_storage, expect=Dimension.VOLUME),
    )


@dataclass
class Prefix:
    length: int
    duration_s: list[float]
    inflow: list[float]
    depth: list[float]
    eco: list[float]
    delivery: dict[str, list[float]]   # 实际（已执行）
    spill: list[float]
    storage_path: list[float]          # 长度 m+1，账面
    measured_final: float | None
    book_final: float
    labels: list[str | None]
    planned_delivery: dict[str, list[float]] | None = None
    planned_inflow: list[float] | None = None


# ---------- 已执行前缀：从实际数据构造，优化器无权改写 ----------

def build_prefix(scn: ScenarioIn, actual: list, curve: StorageCurve) -> Prefix:
    actual = sorted(actual, key=lambda a: a.step_index)
    m = len(actual)
    keys = [d.key for d in scn.demands]
    if not actual:
        raise ScenarioError("滚动重算至少需要 1 个已实际执行时段（actual）")
    idxs = [a.step_index for a in actual]
    if idxs != list(range(m)):
        raise ScenarioError(f"实际前缀必须是时段 0..m-1 的连续前缀，收到 {idxs}")
    if m > len(scn.steps):
        raise ScenarioError("实际时段数超过场景时段数")

    dur, inflow, depth, eco, spill = [], [], [], [], []
    delivery = {k: [0.0] * m for k in keys}
    labels: list[str | None] = []
    v = canonical(scn.initial_storage, expect=Dimension.VOLUME)
    v_path = [v]

    for i, a in enumerate(actual):
        dt = canonical(a.duration, expect=Dimension.TIME)
        dur.append(dt)
        inflow.append(_to_volume(a.inflow, dt, f"时段#{i} 实际入流"))
        depth.append(canonical(a.net_evap_depth, expect=Dimension.LENGTH)
                     if a.net_evap_depth else 0.0)
        eco.append(_q(a.eco_flow, dt) or 0.0)
        spill.append(_q(a.spill, dt) or 0.0)
        labels.append(a.label)
        for k in keys:
            if k in a.delivery:
                delivery[k][i] = _to_volume(a.delivery[k], dt, f"时段#{i} 实际供水({k})")
        # 账面蒸发用当时水位（旧曲线）推求——历史账目不回改
        area = curve.area_at_storage(float(np.clip(v, curve.dead_storage_m3,
                                                   curve.max_storage_m3)))
        e = depth[-1] * area
        d_sum = sum(delivery[k][i] for k in keys)
        v = v + inflow[-1] - e - d_sum - spill[-1]
        v = float(np.clip(v, curve.dead_storage_m3, curve.max_storage_m3))
        v_path.append(v)

    measured = None
    if actual[-1].measured_storage is not None:
        measured = canonical(actual[-1].measured_storage, expect=Dimension.VOLUME)
    return Prefix(m, dur, inflow, depth, eco, delivery, spill, v_path,
                  measured, v_path[-1], labels)


def _extract_planned_prefix(scn: ScenarioIn, confirmed: dict, m: int,
                            durations: list[float]):
    """从已确认快照取出"当初计划的"前 m 个时段供水/入流（用于执行偏差归因）。"""
    res = confirmed.get("result") if isinstance(confirmed, dict) else None
    keys = [d.key for d in scn.demands]
    planned_d = {k: [0.0] * m for k in keys}
    planned_i: list[float] = []
    if res and "ledger" in res:
        ledger = res["ledger"]
        for i in range(m):
            if i < len(ledger):
                row = ledger[i]
                for k in keys:
                    planned_d[k][i] = float(row.get("delivery_m3", {}).get(k, 0.0))
                planned_i.append(float(row.get("inflow_m3", 0.0)))
            else:
                planned_i.append(0.0)
    else:
        # 无快照时退回场景原始输入（旧预测=场景自带序列）
        for i in range(m):
            st = scn.steps[i]
            planned_i.append(_q(st.inflow, durations[i]) or 0.0)
            for j, d in enumerate(scn.demands):
                planned_d[keys[j]][i] = _q(d.amount[i], durations[i]) or 0.0
    return planned_d, planned_i


# ---------- 未来视界：拼接已执行前缀 + 新预测 ----------

def build_future_steps(scn: ScenarioIn, prefix: Prefix, forecast: list,
                       impute: str) -> tuple[list, list[float]]:
    """返回 (forecast 归一化记录, 未来步长)。要求 forecast 覆盖 m..N-1。"""
    forecast = sorted(forecast, key=lambda f: f.step_index)
    n_total = len(scn.steps)
    m = prefix.length
    idxs = [f.step_index for f in forecast]
    if idxs != list(range(m, n_total)):
        raise ScenarioError(
            f"预测必须连续覆盖时段 {m}..{n_total - 1}（当前时刻为 {m}），收到 {idxs}")
    recs, durs = [], []
    for f in forecast:
        dt = canonical(f.duration, expect=Dimension.TIME)
        durs.append(dt)
        recs.append({
            "label": f.label, "duration_s": dt,
            "inflow": _q(f.inflow, dt),
            "depth": canonical(f.net_evap_depth, expect=Dimension.LENGTH)
                     if f.net_evap_depth else 0.0,
            "eco": _q(f.eco_flow, dt) or 0.0,
        })
    if any(r["inflow"] is None for r in recs):
        miss = [forecast[i].step_index for i, r in enumerate(recs) if r["inflow"] is None]
        if impute == "none":
            raise ScenarioError(
                f"预测时段 {miss} 来水缺测；预测缺测也不能当零，请选插补或修订预测")
        values = [r["inflow"] for r in recs]
        imp = impute_inflow(values, ImputeMethod(impute))
        for i, v in enumerate(imp.values_m3):
            recs[i]["inflow"] = v
        imputed_local = imp.imputed_indexes
    else:
        imputed_local = []
    return recs, durs, [i + m for i in imputed_local]


def _future_demands(scn: ScenarioIn, prefix: Prefix,
                    future_dur: list[float]) -> tuple[list[str], np.ndarray, np.ndarray, dict]:
    """未来时段的需求矩阵 [K, Nf]（缺测直接报错——需求侧缺测不用零顶替）。"""
    keys = [d.key for d in scn.demands]
    m = prefix.length
    nf = len(scn.steps) - m
    demand = np.zeros((len(keys), nf))
    min_frac = np.array([d.min_fraction for d in scn.demands])
    for j, d in enumerate(scn.demands):
        for t in range(nf):
            q = d.amount[m + t]
            if q is None or getattr(q, "value", None) is None:
                raise ScenarioError(f"需求 '{d.key}' 在未来时段 #{m + t} 缺测，请补齐")
            demand[j, t] = _q(q, future_dur[t]) or 0.0
    names = {d.key: d.name for d in scn.demands}
    return keys, demand, min_frac, names


def _full_steps(prefix: Prefix, recs: list) -> StepInput:
    """拼出包含已执行前缀的完整 StepInput（仅诊断/说明用）。"""
    inflow = prefix.inflow + [r["inflow"] for r in recs]
    depth = prefix.depth + [r["depth"] for r in recs]
    eco = prefix.eco + [r["eco"] for r in recs]
    avg_dt = (sum(prefix.duration_s) + sum(r["duration_s"] for r in recs)) / len(inflow)
    return StepInput(inflow, depth, eco, avg_dt)


def _commitments_from_confirmed(confirmed: dict, m: int, n_total: int,
                                keys: list[str]) -> tuple[np.ndarray, int, dict]:
    """承诺矩阵（未来段）。来自已确认计划中第 m..N-1 时段的逐户供水。"""
    nf = n_total - m
    commit = np.zeros((len(keys), nf))
    contract = {"notice_steps": 0, "down_cost_multiplier": 10.0, "emergency": False}
    version = None
    if confirmed:
        contract.update(confirmed.get("contract", {}))
        version = confirmed.get("forecast_version")
        ledger = (confirmed.get("result") or {}).get("ledger", [])
        for t in range(nf):
            ai = m + t
            if ai < len(ledger):
                dmap = ledger[ai].get("delivery_m3", {})
                for j, k in enumerate(keys):
                    commit[j, t] = float(dmap.get(k, 0.0))
    return commit, int(contract.get("notice_steps", 0)), contract


# ---------- 输出适配 ----------

def _to_solve_out(scn: ScenarioIn, curve: StorageCurve, labels, durations,
                  res: SolveResult, *, name: str, feasible: bool = True,
                  conflicts=None, prefix_len: int = 0, notice_future: int = 0) -> SolveOut:
    """复用 services 的账目构造，但使用滚动视界自带的标签/步长。"""
    meta = {"duration_s": durations, "labels": labels,
            "imputed_inflow": res.imputed_indexes, "impute_method": res.impute_method,
            "imputed_demand": {}}
    rows, levels = _ledger(scn, curve, meta, res,
                           prefix_len=prefix_len, notice_steps=notice_future)
    from .schemas import SolveOut as SO
    return SO(
        status=res.status if feasible else "infeasible",
        feasible=feasible, name=name,
        weights=res.weights,
        imputed_inflow_indexes=res.imputed_indexes,
        imputed_demand_indexes={},
        impute_method=res.impute_method,
        evap_iterations=res.evap_iterations,
        objective=res.objective, totals=res.totals,
        storage_curve=_curve_dicts(curve), level_m=levels,
        ledger=rows, conflicts=conflicts or [],
        demand_names={d.key: d.name for d in scn.demands},
    )


# ---------- 反事实求解（归因用） ----------

def _counterfactual(curve: StorageCurve, steps: StepInput, keys, names, weights,
                    demand, min_frac, *, initial: float,
                    commitment: np.ndarray, notice: int, mult: float,
                    spillway: float | None, fixed_mask: np.ndarray | None = None,
                    lower: np.ndarray | None = None, emergency: bool = False):
    k, n = demand.shape
    lo = np.zeros((k, n))
    if lower is not None:
        lo = np.maximum(lo, lower)
    if not emergency:
        lo = np.maximum(lo, min_frac[:, None] * demand)
    fixed = fixed_mask if fixed_mask is not None else np.zeros((k, n), dtype=bool)
    h = Horizon(
        keys=keys, names=names, weights=weights, demand_m3=demand,
        min_fraction=min_frac, lower_m3=lo, upper_m3=demand,
        fixed_mask=fixed, commitment_m3=commitment, notice_steps=notice,
        down_cost_multiplier=mult,
    )
    return solve_horizon(curve, steps, h, initial_storage_m3=initial,
                         spillway_capacity_m3=spillway)


def _diff_vec(a: dict[str, list[float]], b: dict[str, list[float]],
              keys: list[str]) -> dict[str, float]:
    """Σ(a - b) 逐户总量差。"""
    return {k: float(sum(a.get(k, [])) - sum(b.get(k, []))) for k in keys}


def _stitch(prefix: Prefix, fut: SolveResult, current_storage: float,
            curve: StorageCurve, recs: list, keys: list[str],
            imputed_global: list[int] | None = None) -> SolveResult:
    """实际前缀（不可变）+ 未来 LP 结果 -> 完整 SolveResult。

    衔接处：未来 LP 从 current_storage（真实当前库容）起算；
    前缀账面末库容与 current_storage 的差已记为 reconciliation，
    不塞进任何时段的平衡残差，保证每个时段自身守恒。
    """
    m = prefix.length
    nf = len(recs)

    storage = prefix.storage_path[:-1] + [current_storage] + fut.storage_m3[1:]
    inflow = prefix.inflow + fut.inflow_m3
    # 前缀蒸发用账面水量反推（保持历史账目自洽）；未来用 LP 蒸发
    evap_prefix = []
    for t in range(m):
        d_sum = sum(prefix.delivery[k][t] for k in keys)
        e = (prefix.storage_path[t] + prefix.inflow[t] - d_sum
             - prefix.spill[t] - prefix.storage_path[t + 1])
        evap_prefix.append(e)
    evaporation = evap_prefix + fut.evaporation_m3
    spill = prefix.spill + fut.spill_m3
    delivery = {k: prefix.delivery[k] + list(fut.delivery_m3[k]) for k in keys}
    demand = {k: ([max(prefix.delivery[k][t], 0.0) for t in range(m)]
                  + list(fut.demand_m3[k]))
              for k in keys}
    deficit = {k: [max(0.0, demand[k][t] - delivery[k][t]) for t in range(m + nf)]
               for k in keys}
    eco_release = [sum(prefix.delivery[k][t] for k in keys) + prefix.spill[t]
                   for t in range(m)] + fut.eco_release_m3
    eco_req = prefix.eco + fut.eco_requirement_m3

    residual = []
    for t in range(m):
        d_sum = sum(delivery[k][t] for k in keys)
        residual.append(prefix.storage_path[t] + inflow[t] - evaporation[t]
                        - d_sum - spill[t] - prefix.storage_path[t + 1])
    # 未来段第一个时段的"期初"是 current_storage，与 fut.storage_m3[0] 一致
    for t in range(m, m + nf):
        ft = t - m
        d_sum = sum(delivery[k][t] for k in keys)
        residual.append(storage[t] + inflow[t] - evaporation[t]
                        - d_sum - spill[t] - storage[t + 1])

    totals = {
        "inflow": float(sum(inflow)),
        "evaporation": float(sum(evaporation)),
        "spill": float(sum(spill)),
        "initial_storage": float(prefix.storage_path[0]),
        "final_storage": float(storage[-1]),
    }
    for k in keys:
        totals[f"delivery_{k}"] = float(sum(delivery[k]))
        totals[f"deficit_{k}"] = float(sum(deficit[k]))
        totals[f"shortfall_{k}"] = float(sum(fut.shortfall_m3[k]))
    totals["commitment_change_cost"] = fut.change_cost_total

    return SolveResult(
        status=fut.status,
        storage_m3=[float(x) for x in storage],
        inflow_m3=[float(x) for x in inflow],
        evaporation_m3=[float(x) for x in evaporation],
        spill_m3=[float(x) for x in spill],
        delivery_m3={k: [float(x) for x in delivery[k]] for k in keys},
        demand_m3={k: [float(x) for x in demand[k]] for k in keys},
        deficit_m3={k: [float(x) for x in deficit[k]] for k in keys},
        eco_release_m3=[float(x) for x in eco_release],
        eco_requirement_m3=[float(x) for x in eco_req],
        weights=fut.weights,
        conservation_residual_m3=[float(x) for x in residual],
        evap_iterations=fut.evap_iterations,
        objective=fut.objective, totals=totals,
        imputed_indexes=list(imputed_global or []), impute_method=fut.impute_method,
        notice_steps=m + fut.notice_steps,
        commitment_m3={k: [0.0] * m + list(fut.commitment_m3[k]) for k in keys},
        shortfall_m3={k: [0.0] * m + list(fut.shortfall_m3[k]) for k in keys},
        change_cost_total=fut.change_cost_total,
        delivery_value=fut.delivery_value,
    )


# ---------- 主流程 ----------

def replan(req, scn: ScenarioIn) -> dict:
    contract = req.contract.model_dump()
    notice = int(contract["notice_steps"])
    mult = float(contract["down_cost_multiplier"])
    emergency = bool(contract["emergency"])
    m = len(req.actual)
    n_total = len(scn.steps)
    keys = [d.key for d in scn.demands]

    old_curve = build_curve(scn)
    prefix = build_prefix(scn, req.actual, old_curve)
    planned_d, planned_i = _extract_planned_prefix(
        scn, req.confirmed, m, prefix.duration_s)
    prefix.planned_delivery = planned_d
    prefix.planned_inflow = planned_i

    recs, future_dur, imputed_global = build_future_steps(
        scn, prefix, req.forecast, req.impute_inflow)
    keys2, demand, min_frac, names = _future_demands(scn, prefix, future_dur)
    assert keys2 == keys

    # 承诺（已确认计划版本绑定）。通知期规则沿用确认时的合同示例，
    # 本次请求只能覆盖紧急豁免等"应对新情况"的参数，不能事后缩短通知期。
    commitment, c_notice, c_contract = _commitments_from_confirmed(
        req.confirmed, m, n_total, keys)
    eff_notice = min(c_notice if req.confirmed else notice, n_total - m)
    mult = float(c_contract.get("down_cost_multiplier", mult)) if req.confirmed else mult
    if req.confirmed:
        # 紧急豁免只能从 false->true（新情况触发），不能用新请求把已确认的豁免取消
        emergency = emergency or bool(c_contract.get("emergency", False))

    # 已执行前缀的固定供水/步长
    all_dur = prefix.duration_s + future_dur
    all_labels = prefix.labels + [r["label"] for r in recs]

    # 起点库容：以实测为准；曲线修订按时段末水位重估。历史账目不回改，
    # 所有差额只在重算起点做一次性"账面调整"。
    curve = old_curve
    reval_gap = 0.0
    revisions: list[ReconciliationOut] = []
    ref_storage = prefix.measured_final if prefix.measured_final is not None else prefix.book_final
    exec_gap = (prefix.measured_final - prefix.book_final) if prefix.measured_final is not None else 0.0
    if exec_gap != 0.0:
        revisions.append(ReconciliationOut(
            period=f"时段#{m - 1}末（实测对账点）",
            book_storage_m3=prefix.book_final,
            measured_storage_m3=prefix.measured_final,
            adjustment_m3=exec_gap,
            reason="实测当前库容与账面不符：历史账目保留不动，差额作为一次性账面调整，"
                   "重算从真实当前库容开始（差额源于未入账的渗漏/测量/执行偏差）"))
    current_storage = ref_storage
    if req.curve_revision is not None:
        new_curve = _curve_from_revision(req.curve_revision)
        if req.curve_revision.convert_initial:
            z = old_curve.elevation(ref_storage)
            current_storage = new_curve.storage(z)
        else:
            current_storage = float(np.clip(ref_storage,
                                            new_curve.dead_storage_m3, new_curve.max_storage_m3))
        reval_gap = current_storage - ref_storage
        revisions.append(ReconciliationOut(
            period=f"时段#{m - 1}末（曲线修订点）",
            book_storage_m3=ref_storage, measured_storage_m3=current_storage,
            adjustment_m3=reval_gap,
            reason=(f"库容曲线修订：当前水位 {z:.3f} m 不变，按新曲线重估库容"
                    "（不是物理水量增减，是量算基准变化）"
                    if req.curve_revision.convert_initial else
                    "库容曲线修订：直接沿用原水量（未按水位换算）")))
        curve = new_curve

    current_storage = float(np.clip(current_storage,
                                    curve.dead_storage_m3, curve.max_storage_m3))

    spillway = None
    if scn.spillway_capacity:
        q = Quant(value=scn.spillway_capacity.value, unit=scn.spillway_capacity.unit)
        spillway = q.canonical() * (all_dur[0] if q.dimension() == Dimension.FLOW else 1.0)

    nf = n_total - m
    k = len(keys)

    # LP 只解未来段（过去不可被优化）；起点为真实当前库容
    future_steps = StepInput(
        inflow_m3=[r["inflow"] for r in recs],
        net_evap_depth_m=[r["depth"] for r in recs],
        eco_flow_m3=[r["eco"] for r in recs],
        duration_s=sum(future_dur) / nf)

    full_lower = np.zeros((k, nf))
    if not emergency:
        full_lower = min_frac[:, None] * demand

    h = Horizon(
        keys=keys, names=names,
        weights={d.key: d.weight for d in scn.demands},
        demand_m3=demand, min_fraction=min_frac,
        lower_m3=full_lower, upper_m3=demand,
        fixed_mask=np.zeros((k, nf), dtype=bool),
        commitment_m3=commitment, notice_steps=eff_notice,
        down_cost_multiplier=mult,
    )

    conflicts = []
    try:
        fut = solve_horizon(curve, future_steps, h,
                            initial_storage_m3=current_storage,
                            spillway_capacity_m3=spillway)
        feasible = True
    except SolveError:
        diag = diagnose(
            curve, future_steps.inflow_m3, future_steps.net_evap_depth_m,
            future_steps.eco_flow_m3,
            {key: list(demand[j]) for j, key in enumerate(keys)},
            weights={key: scn_d(scn, key).weight for key in keys},
            initial_storage_m3=current_storage,
            spillway_capacity_m3=spillway,
            lower_m3={key: list(full_lower[j]) for j, key in enumerate(keys)},
            notice_steps=eff_notice,
            commitment_m3={key: list(commitment[j]) for j, key in enumerate(keys)},
        )
        from .core.optimizer import SolveResult
        fut = SolveResult(
            status="infeasible", storage_m3=diag.storage_m3,
            inflow_m3=future_steps.inflow_m3,
            evaporation_m3=[0.0] * nf,
            spill_m3=diag.spill_m3, delivery_m3=diag.delivery_m3,
            demand_m3={key: list(demand[j]) for j, key in enumerate(keys)},
            deficit_m3=diag.deficit_m3, eco_release_m3=[0.0] * nf,
            eco_requirement_m3=future_steps.eco_flow_m3,
            weights=_norm_weights({key: scn_d(scn, key).weight for key in keys}),
            conservation_residual_m3=diag.forced_loss_m3,
            evap_iterations=0, objective=None)
        feasible = False
        # 诊断的时段编号平移到全局
        conflicts = [{
            "index": c.index + m, "kind": c.kind,
            "message": c.message.replace(f"时段 {c.index}", f"时段 {c.index + m}"),
            "storage_balance_m3": c.storage_balance_m3,
            "storage_bound_m3": c.storage_bound_m3, "gap_m3": c.gap_m3,
        } for c in diag.conflicts]

    # 把已执行前缀与未来计划拼成完整 SolveResult（衔接点用账面调整，不改历史）
    res = _stitch(prefix, fut, current_storage, curve, recs, keys, imputed_global)
    plan = _to_solve_out(scn, curve, all_labels, all_dur, res,
                         name=f"滚动计划 @{req.forecast_version}", feasible=feasible,
                         conflicts=conflicts, prefix_len=m,
                         notice_future=eff_notice).model_dump()

    # ---------- 归因：P0 / A / B 反事实（均只在未来段解） ----------
    p0_delivery = {k: commitment[j].tolist() for j, k in enumerate(keys)}  # 旧承诺=旧计划未来段
    attribution = _attribute(
        scn, old_curve, curve, future_steps, keys, names, demand, min_frac,
        commitment=commitment, notice=eff_notice, mult=mult, emergency=emergency,
        spillway=spillway, initial0=prefix.storage_path[0],
        book_final=prefix.book_final, measured_final=prefix.measured_final,
        reval_gap=reval_gap, current_storage=current_storage,
        old_forecast_steps=_old_forecast_steps(scn, prefix),
        new_delivery={k: res.delivery_m3[k][m:] for k in keys},
        p0_delivery=p0_delivery,
        planned_delivery=planned_d, actual_delivery=prefix.delivery,
        planned_inflow=planned_i, actual_inflow=prefix.inflow,
    )

    # ---------- 多来水情景区间 ----------
    # 区间口径：承诺矩阵、通知期、欠诺单价、紧急豁免、起点、曲线全部与主计划相同，
    # 各情景只改变未来来水。未给概率时只给区间，不输出期望/风险概率。
    ranges = []
    prob_note = ""
    if req.evaluated_scenarios:
        probs = [s.probability for s in req.evaluated_scenarios]
        if not all(p is not None for p in probs):
            prob_note = ("部分或全部来水情景未给概率：只报告各情景下的缺口与欠诺代价区间，"
                         "不输出期望值或风险概率（避免给出看似精确的假概率）。")
        elif any(p < 0 or p > 1 for p in probs) or abs(sum(probs) - 1.0) > 1e-6:
            prob_note = ("所给概率之和不为 1：保留各情景概率原值，仅作情景标签，"
                         "不据此做加权期望计算。")
        for scen in req.evaluated_scenarios:
            ranges.append(_evaluate_range(
                scn, scen, curve, prefix, keys, demand, min_frac, names,
                mult, emergency, current_storage, spillway, req.impute_inflow,
                commitment=commitment, notice=eff_notice))

    actual_ledger = _actual_ledger_rows(prefix, keys)
    return {
        "plan": plan,
        "forecast_version": req.forecast_version,
        "based_on_confirmed_version": (req.confirmed or {}).get("forecast_version"),
        "prefix_len": m,
        "horizon_len": m + nf,
        "contract": {**contract, "effective_notice_steps": eff_notice},
        "actual_ledger": actual_ledger,
        "commitments_m3": {k: [0.0] * m + [float(x) for x in commitment[j]]
                          for j, k in enumerate(keys)},
        "reconciliations": [r.model_dump() for r in revisions],
        "attribution": attribution,
        "ranges": [r.model_dump() for r in ranges],
        "probability_note": prob_note,
        "curve_revision_applied": req.curve_revision is not None,
    }


def scn_d(scn: ScenarioIn, key: str):
    return next(d for d in scn.demands if d.key == key)


def _old_forecast_steps(scn: ScenarioIn, prefix: Prefix) -> StepInput:
    """已确认计划当时对未来的预测 = 场景自带序列中 m 起的原始入流（旧预测版）。"""
    m = prefix.length
    inflow, depth, eco, durs = [], [], [], []
    for t in range(m, len(scn.steps)):
        dt = canonical(scn.steps[t].duration, expect=Dimension.TIME)
        durs.append(dt)
        inflow.append(_q(scn.steps[t].inflow, dt) or 0.0)
        depth.append(canonical(scn.steps[t].net_evap_depth, expect=Dimension.LENGTH)
                     if scn.steps[t].net_evap_depth else 0.0)
        eco.append(_q(scn.steps[t].eco_flow, dt) or 0.0)
    return StepInput(inflow, depth, eco, sum(durs) / max(len(durs), 1))


def _attribute(scn, old_curve, new_curve, future_steps, keys, names, demand,
               min_frac, *, commitment, notice, mult, emergency, spillway,
               initial0, book_final, measured_final, reval_gap, current_storage,
               old_forecast_steps, new_delivery, p0_delivery,
               planned_delivery, actual_delivery, planned_inflow, actual_inflow):
    """把未来供水变化分解为 执行偏差 / 预测改变 / 曲线修订。

    未来供水差异（均在未来视界、当前真实库容起点上解，承诺相同）：
      A   旧预测 + 当前起点（旧曲线）
      B   新预测 + 当前起点（旧曲线）      B-A = 预测改变
      D   新预测 + 当前起点（新曲线）      D-B = 曲线修订
    执行偏差不从"未来供水差"反推（那会和起点变化混在一起），而是用前缀的
    实测账目直接度量：实际供水-计划供水、实际入流-计划入流、实测-账面库容。
    总变化相对已确认旧计划承诺 C（在原始期初求解），故还可能有一块
    "起点状态差"（由执行造成）= A-C，归入 due_to_execution_m3。
    """
    weights = {d.key: d.weight for d in scn.demands}

    def solve(curve, steps, initial, commit, notc):
        return _counterfactual(curve, steps, keys, names, weights, demand, min_frac,
                               initial=initial, commitment=commit, notice=notc,
                               mult=mult, spillway=spillway, emergency=emergency)

    # 执行后的真实起点：执行归因用旧曲线表达（曲线修订前的物理状态）
    base_start = measured_final if measured_final is not None else book_final
    base_start = float(np.clip(base_start,
                               old_curve.dead_storage_m3, old_curve.max_storage_m3))

    def safe(steps, curve, initial, commit, notice_eff):
        try:
            r = solve(curve, steps, initial, commit, notice_eff)
            return {k: r.delivery_m3[k] for k in keys}
        except SolveError:
            return None

    a_del = safe(old_forecast_steps, old_curve, base_start, commitment, notice) or p0_delivery
    b_del = safe(future_steps, old_curve, base_start, commitment, notice) or a_del
    if new_curve is not old_curve:
        d_del = safe(future_steps, new_curve, current_storage, commitment, notice) \
            or new_delivery
    else:
        d_del = b_del

    total = _diff_vec(new_delivery, p0_delivery, keys)
    # A-C：旧预测在"当前起点"与"原始期初起点"下的未来供水差，
    # 正是已执行前缀（多用/少用水）改变起点造成的，计入执行偏差。
    due_exec = _diff_vec(a_del, p0_delivery, keys)
    due_fc = _diff_vec(b_del, a_del, keys)
    due_curve = _diff_vec(d_del, b_del, keys)
    unexpl = {k: total[k] - due_exec[k] - due_fc[k] - due_curve[k] for k in keys}

    exec_gap = (measured_final - book_final) if measured_final is not None else 0.0
    m = len(actual_delivery[keys[0]])
    act_d = {k: [float(x) for x in actual_delivery[k]] for k in keys}
    plan_d = {k: [float(x) for x in planned_delivery.get(k, [0.0] * m)] for k in keys}
    exec_gap_d = {k: float(sum(act_d[k]) - sum(plan_d[k])) for k in keys}

    return AttributionOut(
        total_change_m3=total,
        due_to_forecast_m3=due_fc,
        due_to_execution_m3=due_exec,
        due_to_curve_m3=due_curve,
        unexplained_m3=unexpl,
        current_storage_gap_m3=float(current_storage - initial0),
        current_storage_gap_due_to_execution_m3=float(exec_gap),
        current_storage_gap_due_to_revaluation_m3=float(reval_gap),
        actual_delivery_planned_m3=plan_d,
        actual_delivery_m3=act_d,
        actual_execution_gap_m3=exec_gap_d,
        actual_inflow_planned_m3=[float(x) for x in planned_inflow],
        actual_inflow_m3=[float(x) for x in actual_inflow],
    ).model_dump()


def _evaluate_range(scn, scen, curve, prefix, keys, demand, min_frac, names,
                    mult, emergency, current_storage, spillway, impute,
                    *, commitment: np.ndarray, notice: int,
                    base_curve=None) -> ScenarioRangeOut:
    """同一方案在某来水情景下的缺口/欠诺代价。

    关键：承诺矩阵 commitment 与通知期 notice 必须与主计划完全相同——
    不同情景下变化的只有"来水"，合同规则不能随情景放松，否则区间没有可比性。
    """
    m = prefix.length
    recs, durs, _ = build_future_steps(scn, prefix, scen.steps, impute)
    steps = StepInput(
        inflow_m3=[r["inflow"] for r in recs],
        net_evap_depth_m=[r["depth"] for r in recs],
        eco_flow_m3=[r["eco"] for r in recs],
        duration_s=sum(durs) / len(durs))
    weights = {d.key: d.weight for d in scn.demands}
    try:
        res = _counterfactual(curve, steps, keys, names, weights, demand, min_frac,
                              initial=current_storage,
                              commitment=commitment, notice=notice, mult=mult,
                              spillway=spillway, emergency=emergency)
        deficit = {k: float(sum(res.deficit_m3[k])) for k in keys}
        per_period = {k: [float(x) for x in res.deficit_m3[k]] for k in keys}
        short = {k: float(sum(res.shortfall_m3[k])) for k in keys}
        short_period = {k: [float(x) for x in res.shortfall_m3[k]] for k in keys}
        return ScenarioRangeOut(
            key=scen.key, name=scen.name, probability=scen.probability,
            feasible=True, deficit_total_m3=deficit,
            deficit_range_per_period_m3=per_period,
            shortfall_total_m3=short,
            shortfall_range_per_period_m3=short_period,
            change_cost_total=res.change_cost_total,
            final_storage_m3=res.storage_m3[-1], conflicts_count=0)
    except SolveError:
        # 连硬承诺都无法兑现：缺口按需求、欠诺按承诺量给出区间上界
        raw_w = np.array([weights[k] for k in keys], dtype=float)
        w_norm = raw_w / raw_w.sum()
        cost_mat = np.tile(w_norm * mult, (demand.shape[1], 1)).T
        return ScenarioRangeOut(
            key=scen.key, name=scen.name, probability=scen.probability,
            feasible=False,
            deficit_total_m3={k: float(sum(demand[j])) for j, k in enumerate(keys)},
            deficit_range_per_period_m3={k: demand[j].tolist()
                                         for j, k in enumerate(keys)},
            shortfall_total_m3={k: float(sum(commitment[j])) for j, k in enumerate(keys)},
            shortfall_range_per_period_m3={k: commitment[j].tolist()
                                           for j, k in enumerate(keys)},
            change_cost_total=float((cost_mat * commitment).sum()),
            final_storage_m3=current_storage,
            conflicts_count=1)


def _actual_ledger_rows(prefix: Prefix, keys: list[str]) -> list[dict]:
    rows = []
    for t in range(prefix.length):
        rows.append({
            "index": t,
            "label": prefix.labels[t],
            "duration_s": prefix.duration_s[t],
            "inflow_m3": prefix.inflow[t],
            "inflow_planned_m3": (prefix.planned_inflow or [None] * prefix.length)[t],
            "inflow_execution_gap_m3": prefix.inflow[t] - (prefix.planned_inflow or [0] * prefix.length)[t],
            "delivery_m3": {k: prefix.delivery[k][t] for k in keys},
            "delivery_planned_m3": {k: (prefix.planned_delivery or {}).get(k, [0] * prefix.length)[t]
                                    for k in keys},
            "spill_m3": prefix.spill[t],
            "end_storage_m3_book": prefix.storage_path[t + 1],
            "fixed": True,
        })
    return rows

