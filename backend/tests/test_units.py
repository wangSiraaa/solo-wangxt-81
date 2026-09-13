import math

import pytest

from app.core.units import Dimension, Quant, UnitError, canonical


def test_volume_flow_time_conversion():
    # 1 m³/s × 86400 s = 86400 m³
    assert canonical(1, "m3/s", expect=Dimension.FLOW) * 86400 == pytest.approx(86400)
    # 万m³
    assert Quant(1, "万m3").as_("m3") == pytest.approx(10_000)
    # mm -> m
    assert Quant(3, "mm").as_("m") == pytest.approx(0.003)


def test_dimension_conflict_rejected():
    with pytest.raises(UnitError):
        Quant(1, "m3/s").as_("m3")  # 流量不能直接说成体积（必须先乘时长）
    with pytest.raises(UnitError):
        canonical({"value": 2.5, "unit": "mm"}, expect=Dimension.VOLUME)


def test_missing_value_never_zero():
    with pytest.raises(UnitError):
        canonical(None, "m3/s")
    with pytest.raises(UnitError):
        canonical({"value": None, "unit": "m3"}, expect=Dimension.VOLUME)


def test_area_units():
    assert Quant(1, "ha").as_("m2") == pytest.approx(10_000)
    assert Quant(1, "亩").as_("m2") == pytest.approx(666.6666667, rel=1e-5)
    assert Quant(1, "km2").as_("m2") == pytest.approx(1e6)
