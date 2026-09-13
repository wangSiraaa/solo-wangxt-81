# 库容曲线（水位-库容-水面面积）
#
# 给定一组 (高程 z, 库容 V) 点：
#   V(z)  单调递增、分段线性插值（scipy PchipInterpolator / interp1d 均可，
#         分段线性与教学水量账完全一致，故默认线性）；
#   z(V)  为其反函数；
#   A(z)  若给了面积点则同样分段线性，否则由相邻两点的 ΔV/Δz 几何关系推出：
#         dV = A·dz，所以 A(z) ≈ ΔV/Δz。
#
# 所有长度单位 m、体积单位 m^3、面积单位 m^2（入参先经 units 层换算）。

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import interp1d


class CurveError(ValueError):
    pass


@dataclass(frozen=True)
class CurvePoint:
    elevation_m: float
    storage_m3: float
    area_m2: float | None = None


class StorageCurve:
    def __init__(self, points: list[CurvePoint], *, dead_storage_m3: float,
                 max_storage_m3: float):
        if len(points) < 2:
            raise CurveError("库容曲线至少需要 2 个点")
        z = np.array([p.elevation_m for p in points], dtype=float)
        v = np.array([p.storage_m3 for p in points], dtype=float)
        order = np.argsort(z)
        z, v = z[order], v[order]
        if np.any(np.diff(z) <= 0):
            raise CurveError("高程必须严格递增")
        if np.any(np.diff(v) < 0):
            raise CurveError("库容必须随高程单调不减")
        self._z = z
        self._v = v
        self._min_z, self._max_z = float(z[0]), float(z[-1])
        self._min_v, self._max_v = float(v[0]), float(v[-1])

        areas = [p.area_m2 for p in points]
        if all(a is not None for a in areas):
            a = np.array(areas, dtype=float)[order]
            if np.any(a < 0):
                raise CurveError("水面面积不能为负")
        else:
            # dV = A·dz 的几何反推
            a = np.zeros_like(v)
            for i in range(len(v)):
                if i == 0:
                    a[i] = (v[1] - v[0]) / (z[1] - z[0])
                elif i == len(v) - 1:
                    a[i] = (v[-1] - v[-2]) / (z[-1] - z[-2])
                else:
                    a[i] = ((v[i] - v[i - 1]) / (z[i] - z[i - 1])
                            + (v[i + 1] - v[i]) / (z[i + 1] - z[i])) / 2.0
        self._a = np.maximum(a, 0.0)

        if not (self._min_v <= dead_storage_m3 <= self._max_v):
            raise CurveError("死库容必须落在曲线库容范围内")
        if not (dead_storage_m3 <= max_storage_m3 <= self._max_v):
            raise CurveError("最大蓄水量必须满足 死库容 <= 最大蓄水量 <= 曲线最大库容")
        self.dead_storage_m3 = float(dead_storage_m3)
        self.max_storage_m3 = float(max_storage_m3)

        # 分段线性（外推关闭，越界即报错——硬边界）
        self._v_of_z = interp1d(z, v, kind="linear", bounds_error=True)
        self._z_of_v = interp1d(v, z, kind="linear", bounds_error=True)
        self._a_of_z = interp1d(z, self._a, kind="linear", bounds_error=True)

    # ---- 正向 / 反向换算 ----
    def elevation(self, storage_m3: float) -> float:
        self._check_storage(storage_m3)
        return float(self._z_of_v(storage_m3))

    def storage(self, elevation_m: float) -> float:
        if not (self._min_z - 1e-9 <= elevation_m <= self._max_z + 1e-9):
            raise CurveError(
                f"水位 {elevation_m:.3f} m 超出曲线范围 "
                f"[{self._min_z:.3f}, {self._max_z:.3f}] m"
            )
        return float(self._v_of_z(elevation_m))

    def area_at_storage(self, storage_m3: float) -> float:
        self._check_storage(storage_m3)
        return float(self._a_of_z(self._z_of_v(storage_m3)))

    def _check_storage(self, s: float) -> None:
        if not (self._min_v - 1e-6 <= s <= self._max_v + 1e-6):
            raise CurveError(
                f"库容 {s:,.0f} m³ 超出曲线范围 "
                f"[{self._min_v:,.0f}, {self._max_v:,.0f}] m³"
            )

    # ---- 教学用：蒸发体积（净蒸发水深 × 时段平均水面面积）----
    def evaporation_volume(self, storage_start_m3: float, storage_end_m3: float,
                           net_evap_depth_m: float) -> float:
        """E = e_net · A((V_start+V_end)/2)。净水深可负（降水>蒸发时为补给）。"""
        s_mid = np.clip((storage_start_m3 + storage_end_m3) / 2.0,
                        self._min_v, self._max_v)
        return float(net_evap_depth_m * self._a_of_z(self._z_of_v(s_mid)))

    def as_points(self) -> list[CurvePoint]:
        return [
            CurvePoint(float(z), float(v), float(a))
            for z, v, a in zip(self._z, self._v, self._a)
        ]
