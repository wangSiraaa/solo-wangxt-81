# 有限时间步水量分配求解器
#
# 每个时段 t 的水量平衡（硬守恒，逐笔入账）：
#
#   V_{t+1} = V_t + I_t - E_t - Σ_j D_{t,j} - S_t
#
#   V       蓄水量（m^3），边界：死库容 <= V <= 最大蓄水量（硬条件）
#   I       入流（m^3，已由流量×步长换算并完成缺测处理）
#   E       净蒸发损失（m^3，水面净蒸发水深 × 平均水面面积；可为负=面降雨补给）
#   D_j     第 j 类供水（生活/工业/农业…），可带逐时段上下限；滚动计划里
#           已执行前缀用 fixed 固定（D=实际值，优化器无权改写过去）
#   S       弃水（漫坝/溢洪道），0 <= S <= 溢洪道能力（缺省为不限）
#   R_eco   最低生态流量（m^3/步长，硬条件）：Σ_j D_j + S_t >= R_eco_t
#
# 合同承诺（滚动计划）：
#   承诺量 C_{t,j}（已向用户承诺的后续供水）。
#   - 通知期内（t < notice_steps）：D = C 为硬等式（临时变更已来不及通知）；
#   - 通知期外：D + u >= C，u >= 0 为欠诺缺口；欠诺按合同单价单独计入"改变代价"，
#     目标里 -w·D（供水收益）+ p·u（违约代价），故只要物理上做得到就自动履约；
#   - 紧急豁免 emergency：可降到最低保证比例以下，欠诺代价仍计。
#
# 目标（线性规划，公开权重）：
#   max  Σ_t Σ_j w_j·D_{t,j} - Σ_t Σ_j p_{t,j}·u_{t,j}
#        - ε·Σ S_t + ε·Σ V_{t+1}
#   （后两项仅用于打破多解平局：先尽量不弃水、尽量蓄水，绝不压过供水权重）
#
# E_t 依赖时段末库容（非线性），用定点迭代：假设 E -> 解 LP -> 用曲线重算 E
# -> 再解 LP，直到 E 的变化收敛。找不到可行解时抛出 SolveError，
# 由 diagnose 模块做逐时段冲突定位。

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

from .curve import StorageCurve


class SolveError(RuntimeError):
    def __init__(self, message: str, *, lp_status: int | None = None):
        super().__init__(message)
        self.lp_status = lp_status


@dataclass
class Demand:
    key: str           # life / industry / agriculture ...
    name: str
    weight: float      # 公开权重（相对值即可，求解前归一化）
    amount_m3: list[float]                 # 逐时段需求量
    min_fraction: float = 0.0              # 最低保证比例（0 表示无硬下限）


@dataclass
class StepInput:
    inflow_m3: list[float]                 # 已完成单位换算与缺测处理
    net_evap_depth_m: list[float]          # 逐时段净蒸发水深（蒸发-降水）
    eco_flow_m3: list[float]               # 逐时段最低生态水量（硬）
    duration_s: float                      # 步长（秒，用于说明，不参与平衡）


@dataclass
class Horizon:
    """滚动计划用：逐时段供水边界与承诺矩阵（长度均为 N；K 类用户）。"""
    keys: list[str]
    names: dict[str, str]
    weights: dict[str, float]
    demand_m3: np.ndarray                  # [K, N]
    min_fraction: np.ndarray               # [K]
    lower_m3: np.ndarray                   # [K, N] 供水硬下限
    upper_m3: np.ndarray                   # [K, N] 供水硬上限（=需求量）
    fixed_mask: np.ndarray                 # [K, N] True=已执行，D 固定
    commitment_m3: np.ndarray              # [K, N] 承诺量 C
    notice_steps: int = 0                  # 通知期（自视界起点的时段数）
    shortfall_cost: np.ndarray | None = None   # [K, N] 欠诺单价 p；None=默认
    down_cost_multiplier: float = 10.0     # 默认欠诺单价 = 该倍数 × 归一化权重


@dataclass
class SolveResult:
    status: str
    storage_m3: list[float]                # 长度 N+1（含初始值）
    inflow_m3: list[float]
    evaporation_m3: list[float]
    spill_m3: list[float]
    delivery_m3: dict[str, list[float]]    # key -> 长度 N
    demand_m3: dict[str, list[float]]
    deficit_m3: dict[str, list[float]]
    eco_release_m3: list[float]            # ΣD + S（实际下泄生态水量）
    eco_requirement_m3: list[float]
    weights: dict[str, float]              # 归一化后的公开权重
    conservation_residual_m3: list[float]  # 逐时段平衡残差（应≈0）
    evap_iterations: int
    objective: float
    totals: dict[str, float] = field(default_factory=dict)
    imputed_indexes: list[int] = field(default_factory=list)
    impute_method: str = "none"
    # ---- 滚动计划扩展 ----
    notice_steps: int = 0
    commitment_m3: dict[str, list[float]] = field(default_factory=dict)
    shortfall_m3: dict[str, list[float]] = field(default_factory=dict)
    change_cost_total: float = 0.0         # Σ p·u（承诺改变代价，单独列示）
    delivery_value: float = 0.0            # Σ w·D


_EPS_SPILL = 1e-6     # 弃水惩罚（相对权重，仅破平局）
_EPS_STORE = 1e-7     # 蓄水偏好（仅破平局）
_FP_TOL = 1e-3        # m^3，蒸发定点迭代收敛阈值
_FP_MAX_ITER = 20


def solve(curve: StorageCurve, steps: StepInput, demands: list[Demand], *,
          initial_storage_m3: float, final_storage_m3: float | None = None,
          spillway_capacity_m3: float | None = None,
          imputed_indexes: list[int] | None = None,
          impute_method: str = "none") -> SolveResult:
    """一次性计划（兼容原接口）：无已执行前缀、无承诺。"""
    n = len(steps.inflow_m3)
    k = len(demands)
    keys = [d.key for d in demands]
    demand = np.array([d.amount_m3 for d in demands], dtype=float)
    min_frac = np.array([d.min_fraction for d in demands], dtype=float)
    horizon = Horizon(
        keys=keys,
        names={d.key: d.name for d in demands},
        weights={d.key: d.weight for d in demands},
        demand_m3=demand,
        min_fraction=min_frac,
        lower_m3=min_frac[:, None] * demand,
        upper_m3=demand,
        fixed_mask=np.zeros((k, n), dtype=bool),
        commitment_m3=np.zeros((k, n)),
        notice_steps=0,
    )
    return solve_horizon(
        curve, steps, horizon,
        initial_storage_m3=initial_storage_m3,
        final_storage_m3=final_storage_m3,
        spillway_capacity_m3=spillway_capacity_m3,
        imputed_indexes=imputed_indexes, impute_method=impute_method,
    )


def solve_horizon(curve: StorageCurve, steps: StepInput, h: Horizon, *,
                  initial_storage_m3: float, final_storage_m3: float | None = None,
                  spillway_capacity_m3: float | None = None,
                  imputed_indexes: list[int] | None = None,
                  impute_method: str = "none") -> SolveResult:
    n = len(steps.inflow_m3)
    if n == 0:
        raise SolveError("时间步序列为空")
    k = len(h.keys)
    keys = h.keys
    if h.demand_m3.shape != (k, n):
        raise SolveError(f"需求矩阵形状 {h.demand_m3.shape} 与用户数/时段数 ({k},{n}) 不一致")
    for name, m in (("下限", h.lower_m3), ("上限", h.upper_m3),
                    ("承诺", h.commitment_m3)):
        if m.shape != (k, n):
            raise SolveError(f"{name}矩阵形状应为 ({k},{n})")
    if not (0 <= h.notice_steps <= n):
        raise SolveError("通知期长度必须在 [0, 时段数]")
    if not (curve.dead_storage_m3 <= initial_storage_m3 <= curve.max_storage_m3):
        raise SolveError(
            f"初始库容 {initial_storage_m3:,.0f} m³ 超出允许边界 "
            f"[{curve.dead_storage_m3:,.0f}, {curve.max_storage_m3:,.0f}] m³"
        )

    raw_w = np.array([h.weights[key] for key in keys], dtype=float)
    if np.any(raw_w < 0) or raw_w.sum() <= 0:
        raise SolveError("权重必须非负且至少一个为正（权重需公开、可比）")
    w = raw_w / raw_w.sum()
    weights_norm = {key: float(x) for key, x in zip(keys, w)}

    inflow = np.array(steps.inflow_m3, dtype=float)
    depth = np.array(steps.net_evap_depth_m, dtype=float)
    eco = np.maximum(np.array(steps.eco_flow_m3, dtype=float), 0.0)
    demand = np.maximum(h.demand_m3, 0.0)
    lower = np.maximum(h.lower_m3, 0.0)
    upper = np.maximum(h.upper_m3, 0.0)
    fixed = h.fixed_mask.astype(bool)
    commit = np.maximum(h.commitment_m3, 0.0)
    if np.any(lower > upper + 1e-6):
        raise SolveError("存在供水下限大于上限的时段，请检查承诺/最低保证设置")
    if np.any(fixed & (lower > upper + 1e-6)):
        raise SolveError("已执行前缀的实际供水量超出当期需求量，无法固定")

    # 欠诺单价：默认 = 倍数 × 归一化权重（远大于供水边际收益，故能履约必履约）
    if h.shortfall_cost is None:
        cost = np.tile(w * h.down_cost_multiplier, (n, 1)).T  # [K,N]
    else:
        cost = np.maximum(h.shortfall_cost, 0.0)
        if cost.shape != (k, n):
            raise SolveError(f"欠诺单价矩阵形状应为 ({k},{n})")
    notice = h.notice_steps
    # 通知期内承诺硬固定；通知期外承诺按 D+u>=C 履约（欠诺付费，可到 0）
    for t in range(notice):
        for j in range(k):
            if commit[j, t] > 0:
                fixed[j, t] = True
                lower[j, t] = upper[j, t] = commit[j, t]
    # 紧急豁免 emergency 体现在调用方传入的 lower_m3（去掉最低保证比例硬下限），
    # 求解器本身只尊重 lower/upper/fixed 三类边界。

    spill_cap = np.inf if spillway_capacity_m3 is None else float(spillway_capacity_m3)
    if spill_cap < 0:
        raise SolveError("溢洪道能力不能为负")

    # ---- 变量布局：每时段块 [D_0..D_{K-1}, S, V_{t+1}, u_0..u_{K-1}] ----
    block = 2 * k + 2
    nv = n * block

    def idx_d(t, j):
        return t * block + j

    def idx_s(t):
        return t * block + k

    def idx_v(t):
        return t * block + k + 1

    def idx_u(t, j):
        return t * block + k + 2 + j

    c = np.zeros(nv)
    for t in range(n):
        for j in range(k):
            c[idx_d(t, j)] = -w[j]
            c[idx_u(t, j)] = cost[j, t]
        c[idx_s(t)] = _EPS_SPILL
        c[idx_v(t)] = -_EPS_STORE

    bounds: list[tuple[float | None, float | None]] = [(None, None)] * nv
    for t in range(n):
        for j in range(k):
            bounds[idx_d(t, j)] = (float(lower[j, t]), float(upper[j, t]))
            cu = commit[j, t] if t >= notice else 0.0
            bounds[idx_u(t, j)] = (0.0, float(cu))
        bounds[idx_s(t)] = (0.0, None if np.isinf(spill_cap) else spill_cap)
        bounds[idx_v(t)] = (curve.dead_storage_m3, curve.max_storage_m3)

    a_eq = np.zeros((n + (1 if final_storage_m3 is not None else 0), nv))
    b_eq = np.zeros(a_eq.shape[0])
    for t in range(n):
        for j in range(k):
            a_eq[t, idx_d(t, j)] = 1.0
        a_eq[t, idx_s(t)] = 1.0
        a_eq[t, idx_v(t)] = 1.0
        if t > 0:
            a_eq[t, idx_v(t - 1)] = -1.0
    if final_storage_m3 is not None:
        if not (curve.dead_storage_m3 <= final_storage_m3 <= curve.max_storage_m3):
            raise SolveError(f"期末目标库容 {final_storage_m3:,.0f} m³ 超出允许边界")
        a_eq[n, idx_v(n - 1)] = 1.0
        b_eq[n] = float(final_storage_m3)

    # 不等式：生态 + 承诺履约
    n_commit_rows = sum(1 for t in range(notice, n) for j in range(k)
                        if commit[j, t] > 0)
    a_ub = np.zeros((n + n_commit_rows, nv))
    b_ub = np.zeros(n + n_commit_rows)
    for t in range(n):  # 生态：ΣD + S >= eco
        for j in range(k):
            a_ub[t, idx_d(t, j)] = -1.0
        a_ub[t, idx_s(t)] = -1.0
        b_ub[t] = -eco[t]
    row = n
    for t in range(notice, n):
        for j in range(k):
            if commit[j, t] > 0:  # D_j + u_j >= C_j
                a_ub[row, idx_d(t, j)] = -1.0
                a_ub[row, idx_u(t, j)] = -1.0
                b_ub[row] = -commit[j, t]
                row += 1

    # ---- 蒸发定点迭代 ----
    storage_guess = np.linspace(
        initial_storage_m3,
        final_storage_m3 if final_storage_m3 is not None
        else min(curve.max_storage_m3, initial_storage_m3 + inflow.sum() / max(n, 1)),
        n + 1,
    )
    storage_guess = np.clip(storage_guess, curve.dead_storage_m3, curve.max_storage_m3)
    evap = _evaporation(curve, storage_guess, depth)

    lp = None
    last_fail = None
    for it in range(1, _FP_MAX_ITER + 1):
        for t in range(n):
            b_eq[t] = inflow[t] - evap[t] + (initial_storage_m3 if t == 0 else 0.0)
        lp = linprog(c, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq,
                     bounds=bounds, method="highs")
        if lp.success:
            new_v = np.array([initial_storage_m3] +
                             [lp.x[idx_v(t)] for t in range(n)])
            new_evap = _evaporation(curve, new_v, depth)
            delta = float(np.max(np.abs(new_evap - evap)))
            storage_guess, evap = new_v, new_evap
            last_fail = None
            if it > 1 and delta <= _FP_TOL:
                break
        else:
            last_fail = int(lp.status)
            # 前向投影：仅用硬边界推出一个库容路径，供重算蒸发
            proj = np.zeros(n + 1)
            proj[0] = initial_storage_m3
            for t in range(n):
                d_fixed = sum(lower[j, t] for j in range(k))
                proj[t + 1] = np.clip(
                    proj[t] + inflow[t] - evap[t] - eco[t] - d_fixed,
                    curve.dead_storage_m3, curve.max_storage_m3,
                )
            storage_guess = proj
            evap = _evaporation(curve, proj, depth)
    else:  # pragma: no cover
        raise SolveError("蒸发定点迭代未收敛，请缩小步长或检查净蒸发水深量级")

    if not lp.success:
        raise SolveError(
            f"线性规划无可行解（HiGHS status={last_fail}）；"
            f"请查看冲突时段诊断（flood=洪峰超容 / drought=生态与死库容冲突 / commitment=承诺无法兑现）",
            lp_status=last_fail,
        )

    x = lp.x
    v = np.array([initial_storage_m3] + [x[idx_v(t)] for t in range(n)])
    spill = np.array([max(0.0, x[idx_s(t)]) for t in range(n)])
    delivery = {
        keys[j]: np.array([max(0.0, x[idx_d(t, j)]) for t in range(n)])
        for j in range(k)
    }
    short = {
        keys[j]: np.array([max(0.0, x[idx_u(t, j)]) if t >= notice else 0.0
                           for t in range(n)])
        for j in range(k)
    }
    deficit = {
        keys[j]: np.maximum(demand[j] - delivery[keys[j]], 0.0)
        for j in range(k)
    }
    eco_release = np.sum(np.array([delivery[key] for key in keys]), axis=0) + spill
    residual = np.array([
        v[t] + inflow[t] - evap[t]
        - np.sum([delivery[keys[j]][t] for j in range(k)])
        - spill[t] - v[t + 1]
        for t in range(n)
    ])
    change_cost = float(sum(cost[j, t] * short[keys[j]][t]
                            for t in range(n) for j in range(k)))
    delivery_value = float(sum(w[j] * delivery[keys[j]][t]
                               for t in range(n) for j in range(k)))

    totals = {
        "inflow": float(inflow.sum()),
        "evaporation": float(evap.sum()),
        "spill": float(spill.sum()),
        "initial_storage": float(v[0]),
        "final_storage": float(v[-1]),
    }
    for key in keys:
        totals[f"delivery_{key}"] = float(delivery[key].sum())
        totals[f"deficit_{key}"] = float(deficit[key].sum())
        totals[f"shortfall_{key}"] = float(short[key].sum())
    totals["commitment_change_cost"] = change_cost

    return SolveResult(
        status="optimal",
        storage_m3=[float(x) for x in v],
        inflow_m3=[float(x) for x in inflow],
        evaporation_m3=[float(x) for x in evap],
        spill_m3=[float(x) for x in spill],
        delivery_m3={key: [float(x) for x in delivery[key]] for key in keys},
        demand_m3={keys[j]: [float(x) for x in demand[j]] for j in range(k)},
        deficit_m3={key: [float(x) for x in deficit[key]] for key in keys},
        eco_release_m3=[float(x) for x in eco_release],
        eco_requirement_m3=[float(x) for x in eco],
        weights=weights_norm,
        conservation_residual_m3=[float(x) for x in residual],
        evap_iterations=it,
        objective=float(-lp.fun),
        totals=totals,
        imputed_indexes=list(imputed_indexes or []),
        impute_method=impute_method,
        notice_steps=notice,
        commitment_m3={key: [float(commit[j, t]) for t in range(n)]
                       for j, key in enumerate(keys)},
        shortfall_m3={key: [float(x) for x in short[key]] for key in keys},
        change_cost_total=change_cost,
        delivery_value=delivery_value,
    )


def _evaporation(curve: StorageCurve, v: np.ndarray, depth: np.ndarray) -> np.ndarray:
    return np.array([
        curve.evaporation_volume(v[t], v[t + 1], depth[t])
        for t in range(len(depth))
    ])
