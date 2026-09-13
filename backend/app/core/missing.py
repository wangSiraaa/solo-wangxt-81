# 缺测来水处理
#
# 原则：缺测(None)绝不当作零。
#   - 不插补（默认）：直接抛 MissingInflowError，API 返回 422 并列出缺测时段；
#   - 显式插补：linear（前后非缺测点线性插值，端点用最近值）/ mean（全序列均值）/
#               zero（用户显式声明"按零处理"，仍会打上 imputed 标记）。
# 插补过的时段一律打标，水量账与前端图表都能追到"这个数不是实测"。

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np


class ImputeMethod(str, Enum):
    NONE = "none"
    LINEAR = "linear"
    MEAN = "mean"
    ZERO = "zero"  # 仅当用户显式选择时使用；与"缺测静默当零"不同


class MissingInflowError(ValueError):
    def __init__(self, missing_indexes: list[int]):
        self.missing_indexes = missing_indexes
        super().__init__(
            f"第 {missing_indexes} 个时段来水缺测；缺测来水不能当作零，"
            f"请选择显式插补方式(linear/mean/zero)或补测后重算"
        )


@dataclass(frozen=True)
class ImputeResult:
    values_m3: list[float]
    imputed_indexes: list[int]
    method: ImputeMethod


def impute_inflow(values_m3: list[float | None],
                  method: ImputeMethod = ImputeMethod.NONE) -> ImputeResult:
    arr = np.array([np.nan if v is None else float(v) for v in values_m3])
    missing = [i for i, v in enumerate(values_m3) if v is None]

    if not missing:
        return ImputeResult([float(x) for x in arr], [], method)

    if method == ImputeMethod.NONE:
        raise MissingInflowError(missing)

    if method == ImputeMethod.ZERO:
        filled = np.where(np.isnan(arr), 0.0, arr)
    elif method == ImputeMethod.MEAN:
        if np.isnan(arr).all():
            raise MissingInflowError(missing)
        filled = np.where(np.isnan(arr), np.nanmean(arr), arr)
    elif method == ImputeMethod.LINEAR:
        idx = np.arange(len(arr))
        good = ~np.isnan(arr)
        if good.sum() < 2:
            # 少于两个观测点无法插值，退化为唯一观测值/零
            fill = float(arr[good][0]) if good.any() else 0.0
            filled = np.where(np.isnan(arr), fill, arr)
        else:
            filled = np.interp(idx, idx[good], arr[good])
    else:  # pragma: no cover
        raise ValueError(f"未知插补方式 {method}")

    return ImputeResult([float(x) for x in filled], missing, method)
