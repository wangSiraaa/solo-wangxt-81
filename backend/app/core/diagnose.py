# 不可行性诊断：逐时段前向模拟
#
# LP 报告无可行解时，用最直观的前向水量账定位"从哪个时段开始对不上"：
#
#   1) 先满足硬条件：生态流量(计入供水+弃水)、溢洪道过流；
#   2) 再按公开权重优先供水（贪心：单位水量边际收益 = 权重，先满足高权重需求）；
#   3) 算出平衡库容 V*；
#         V* > 最大库容：若溢洪道已满 -> 记一条 flood 冲突（洪峰超过可容纳空间）；
#                        否则继续弃水至容量上限；
#         V* < 死库容：记一条 drought 冲突（生态流量+最低保证也无法维持），
#                      库容被夹到死库容，账目上记 forced_deficit。
#
# 蒸发同样做一轮定点修正。诊断结果不是"方案"，只是冲突解释。

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .curve import StorageCurve


@dataclass
class Conflict:
    index: int
    kind: str            # 'flood' | 'drought'
    message: str
    storage_balance_m3: float
    storage_bound_m3: float
    gap_m3: float


@dataclass
class Diagnostic:
    feasible: bool
    conflicts: list[Conflict]
    storage_m3: list[float]
    spill_m3: list[float]
    delivery_m3: dict[str, list[float]]
    deficit_m3: dict[str, list[float]]
    forced_loss_m3: list[float]          # 夹边界时被迫"消失/凭空"的水量（守恒缺口）


def diagnose(curve: StorageCurve, inflow_m3, net_evap_depth_m, eco_m3,
             demand_amounts: dict[str, list[float]], weights: dict[str, float],
             *, initial_storage_m3: float,
             spillway_capacity_m3: float | None = None,
             final_storage_m3: float | None = None,
             lower_m3: dict[str, list[float]] | None = None,
             notice_steps: int = 0,
             commitment_m3: dict[str, list[float]] | None = None) -> Diagnostic:
    n = len(inflow_m3)
    keys = list(demand_amounts.keys())
    inflow = np.asarray(inflow_m3, dtype=float)
    depth = np.asarray(net_evap_depth_m, dtype=float)
    eco = np.maximum(np.asarray(eco_m3, dtype=float), 0.0)
    demands = {k: np.maximum(np.asarray(v, dtype=float), 0.0) for k, v in demand_amounts.items()}
    floors = {k: np.maximum(np.asarray((lower_m3 or {}).get(k, [0.0] * n), dtype=float), 0.0)
              for k in keys}
    commits = {k: np.maximum(np.asarray((commitment_m3 or {}).get(k, [0.0] * n), dtype=float), 0.0)
               for k in keys}
    w = np.array([weights[k] for k in keys], dtype=float)
    cap = np.inf if spillway_capacity_m3 is None else float(spillway_capacity_m3)

    v = np.zeros(n + 1)
    v[0] = initial_storage_m3
    spill = np.zeros(n)
    delivery = {k: np.zeros(n) for k in keys}
    forced = np.zeros(n)
    conflicts: list[Conflict] = []

    for fix_round in range(2):  # 蒸发定点修正两轮，教学诊断足够精确
        conflicts = []
        forced = np.zeros(n)
        for t in range(n):
            area = curve.area_at_storage(
                float(np.clip(v[t], curve.dead_storage_m3, curve.max_storage_m3))
            )
            evap = depth[t] * area
            available = v[t] + inflow[t] - evap

            # 硬下限（最低保证 + 通知期内不可更改的承诺）先放
            required = {k: (commits[k][t] if t < notice_steps and commits[k][t] > 0
                            else floors[k][t]) for k in keys}
            released = 0.0
            for k in keys:
                required[k] = min(required[k], demands[k][t])
                delivery[k][t] = required[k]
                released += required[k]

            # 剩余水量按权重贪心加给各类用户（上限=需求量）
            order = sorted(range(len(keys)), key=lambda j: -w[j])
            for j in order:
                k = keys[j]
                room_demand = demands[k][t] - delivery[k][t]
                give = min(room_demand, max(0.0, available - released))
                delivery[k][t] += give
                released += give

            # 硬下限本身已击穿：单独记一条承诺/保证冲突
            if released > available + 1e-6:
                gap = released - available
                conflicts.append(Conflict(
                    index=t, kind="commitment",
                    message=(f"时段 {t}：即便击穿死库容，硬承诺/最低保证仍缺水 "
                             f"{gap:,.0f} m³（通知期承诺 {sum(commits[k][t] for k in keys):,.0f} "
                             f"m³ 无法兑现）"),
                    storage_balance_m3=float(available - released),
                    storage_bound_m3=curve.dead_storage_m3, gap_m3=float(gap),
                ))

            # 生态不足部分由弃水补足（弃水本身也在下游生态口径内）
            eco_extra = max(0.0, eco[t] - released)
            eco_extra = min(eco_extra, max(0.0, available - released))
            s_eco = eco_extra
            released += s_eco

            balance = available - released  # 此时尚未计入额外弃水的蓄水量

            # 3) 超出最高库容 -> 弃水，受溢洪道能力限制（生态弃水占用了部分能力）
            extra_spill = 0.0
            if balance > curve.max_storage_m3:
                room_spill = max(0.0, cap - s_eco) if not np.isinf(cap) else np.inf
                extra_spill = min(balance - curve.max_storage_m3, room_spill)
            s_total = s_eco + extra_spill
            v_next = available - (released - s_eco + s_total)
            spill[t] = s_total

            if v_next > curve.max_storage_m3 + 1e-6:
                gap = v_next - curve.max_storage_m3
                conflicts.append(Conflict(
                    index=t, kind="flood",
                    message=(f"时段 {t}：即使溢洪道满弃 {cap if not np.isinf(cap) else 0:,.0f} m³，"
                             f"平衡库容仍超最高蓄水量约 {gap:,.0f} m³（洪峰超过可容纳空间）"),
                    storage_balance_m3=float(v_next),
                    storage_bound_m3=curve.max_storage_m3,
                    gap_m3=float(gap),
                ))
                forced[t] = gap
                v_next = curve.max_storage_m3
            elif v_next < curve.dead_storage_m3 - 1e-6:
                gap = curve.dead_storage_m3 - v_next
                conflicts.append(Conflict(
                    index=t, kind="drought",
                    message=(f"时段 {t}：在维持最低生态流量 {eco[t]:,.0f} m³ 后，"
                             f"平衡库容低于死库容约 {gap:,.0f} m³（来水不足以同时保住生态与库容边界）"),
                    storage_balance_m3=float(v_next),
                    storage_bound_m3=curve.dead_storage_m3,
                    gap_m3=float(gap),
                ))
                forced[t] = -gap
                v_next = curve.dead_storage_m3
            v[t + 1] = v_next

        if final_storage_m3 is not None and abs(v[-1] - final_storage_m3) > 1.0:
            conflicts.append(Conflict(
                index=n - 1, kind="target",
                message=(f"期末库容 {v[-1]:,.0f} m³ 无法达到指定目标 "
                         f"{final_storage_m3:,.0f} m³，差 {abs(final_storage_m3 - v[-1]):,.0f} m³"),
                storage_balance_m3=float(v[-1]),
                storage_bound_m3=float(final_storage_m3),
                gap_m3=float(abs(final_storage_m3 - v[-1])),
            ))

    deficit = {k: np.maximum(demands[k] - delivery[k], 0.0) for k in keys}
    return Diagnostic(
        feasible=len(conflicts) == 0,
        conflicts=conflicts,
        storage_m3=[float(x) for x in v],
        spill_m3=[float(x) for x in spill],
        delivery_m3={k: [float(x) for x in delivery[k]] for k in keys},
        deficit_m3={k: [float(x) for x in deficit[k]] for k in keys},
        forced_loss_m3=[float(x) for x in forced],
    )
