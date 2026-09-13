# 单位换算层
#
# 系统内部（canonical）单位：
#   体积 volume  -> m^3
#   流量 flow    -> m^3/s
#   长度 length  -> m
#   面积 area    -> m^2
#   时长 time    -> s
#
# 蒸发水深蒸发深度 -> m（mm 等在换算时转成 m，乘以面积 m^2 得到体积 m^3）
#
# 入参一律带单位标签（如 {"value": 12.5, "unit": "m3/s"}），
# 不允许"猜单位"；无法归类的单位会报错，而不是静默当作零。

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Union


class Dimension(str, Enum):
    VOLUME = "volume"
    FLOW = "flow"
    LENGTH = "length"
    AREA = "area"
    TIME = "time"


# 各单位到 canonical 单位的线性因子：canonical_value = value * factor
_FACTORS: dict[str, tuple[Dimension, float]] = {
    # 体积
    "m3": (Dimension.VOLUME, 1.0),
    "m^3": (Dimension.VOLUME, 1.0),
    "l": (Dimension.VOLUME, 1e-3),
    "L": (Dimension.VOLUME, 1e-3),
    "万m3": (Dimension.VOLUME, 1e4),
    "10^4 m3": (Dimension.VOLUME, 1e4),
    "km3": (Dimension.VOLUME, 1e9),
    # 流量
    "m3/s": (Dimension.FLOW, 1.0),
    "m^3/s": (Dimension.FLOW, 1.0),
    "m3/h": (Dimension.FLOW, 1.0 / 3600.0),
    "m3/d": (Dimension.FLOW, 1.0 / 86400.0),
    "m3/月": (Dimension.FLOW, 1.0 / 2_592_000.0),
    "l/s": (Dimension.FLOW, 1e-3),
    # 长度 / 水深
    "m": (Dimension.LENGTH, 1.0),
    "mm": (Dimension.LENGTH, 1e-3),
    "cm": (Dimension.LENGTH, 1e-2),
    "km": (Dimension.LENGTH, 1e3),
    # 面积
    "m2": (Dimension.AREA, 1.0),
    "m^2": (Dimension.AREA, 1.0),
    "ha": (Dimension.AREA, 1e4),
    "km2": (Dimension.AREA, 1e6),
    "亩": (Dimension.AREA, 10_000.0 / 15.0),  # 1 亩 = 1/15 公顷 ≈ 666.67 m²
    # 时间
    "s": (Dimension.TIME, 1.0),
    "min": (Dimension.TIME, 60.0),
    "h": (Dimension.TIME, 3600.0),
    "d": (Dimension.TIME, 86400.0),
    "月": (Dimension.TIME, 2_592_000.0),  # 30 d，仅用于演示
}

# 每个单位隐含的量纲（供反向校验）
DIMENSION_OF: dict[str, Dimension] = {u: d for u, (d, _) in _FACTORS.items()}


@dataclass(frozen=True)
class Quant:
    """带单位的标量。canonical() 返回内部单位数值。"""

    value: float
    unit: str

    def dimension(self) -> Dimension:
        if self.unit not in _FACTORS:
            raise UnitError(f"未知单位 '{self.unit}'，支持的单位见 units.supported()")
        return _FACTORS[self.unit][0]

    def canonical(self) -> float:
        if self.unit not in _FACTORS:
            raise UnitError(f"未知单位 '{self.unit}'，支持的单位见 units.supported()")
        if self.value is None:
            raise UnitError("数值缺失(None)，不能参与单位换算；缺测需先显式插补或报错")
        return float(self.value) * _FACTORS[self.unit][1]

    def as_(self, unit: str) -> float:
        target = _FACTORS.get(unit)
        if target is None:
            raise UnitError(f"未知目标单位 '{unit}'")
        src = _FACTORS[self.unit]
        if src[0] != target[0]:
            raise UnitError(
                f"量纲冲突：'{self.unit}'({src[0].value}) 不能换算为 '{unit}'({target[0].value})"
            )
        return float(self.value) * src[1] / target[1]


class UnitError(ValueError):
    pass


Number = Union[int, float, dict, "Quant"]


def canonical(value: Number, unit: str | None = None, *, expect: Dimension | None = None) -> float:
    """把 数字+单位 / {'value':..,'unit':..} / Quant 统一换算为内部单位。

    None 直接抛出 UnitError（缺测来水绝不当零）。
    """
    if value is None:
        raise UnitError("数值为 None（缺测）：缺测来水不允许静默按零处理，请先显式插补或退回用户")
    if isinstance(value, Quant):
        q = value
    elif isinstance(value, dict):
        if "unit" not in value or "value" not in value:
            raise UnitError(f"带单位输入必须同时包含 value 与 unit，收到 {value!r}")
        q = Quant(value=value["value"], unit=value["unit"])
    elif hasattr(value, "value") and hasattr(value, "unit"):
        # pydantic QuantIn 等鸭子类型
        q = Quant(value=value.value, unit=value.unit)
    else:
        if unit is None:
            raise UnitError("裸数字必须显式给出 unit")
        q = Quant(value=float(value), unit=unit)
    if expect is not None and q.dimension() != expect:
        raise UnitError(
            f"期望量纲 {expect.value}，实际为 {q.dimension().value}（单位 {q.unit}）"
        )
    return q.canonical()


def supported() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for u, (dim, _) in _FACTORS.items():
        out.setdefault(dim.value, []).append(u)
    return out
