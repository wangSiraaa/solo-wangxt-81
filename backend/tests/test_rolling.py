"""滚动计划测试：前缀固定、承诺代价、预测/执行/曲线归因、情景区间。"""
import copy

import pytest

from app import demos
from app.rolling_schemas import (ActualStepIn, ContractIn, CurveRevisionIn,
                                 ForecastStepIn, ReplanRequest,
                                 ScenarioForecastIn)
from app.rolling import replan
from app.schemas import QuantIn, ScenarioIn


def q(v, u):
    return QuantIn(value=v, unit=u)


def _future_forecast(scn_payload, start, end, scale=1.0):
    """从场景原始序列构造未来预测（即"旧预测"）。"""
    out = []
    for t in range(start, end):
        st = scn_payload["steps"][t]
        out.append(ForecastStepIn(
            step_index=t, label=st.get("label"),
            duration=st["duration"],
            inflow=q(round(st["inflow"]["value"] * scale, 4), st["inflow"]["unit"]),
            net_evap_depth=st.get("net_evap_depth"),
            eco_flow=st.get("eco_flow")))
    return out


def _actual_step(scn_payload, t, *, inflow_scale=1.0, delivery_override=None,
                 measured_storage=None, spill=None):
    st = scn_payload["steps"][t]
    base = st["inflow"]["value"] * inflow_scale
    delivery = delivery_override or {}
    return ActualStepIn(
        step_index=t, label=st.get("label"), duration=st["duration"],
        inflow=q(round(base, 4), st["inflow"]["unit"]),
        net_evap_depth=st.get("net_evap_depth"),
        eco_flow=st.get("eco_flow"),
        delivery={k: q(v, "万m3") for k, v in delivery.items()},
        spill=q(spill, "万m3") if spill is not None else None,
        measured_storage=q(measured_storage, "万m3") if measured_storage is not None else None,
    )


@pytest.fixture
def base_payload():
    return demos.scenario_base()


def _first_plan(payload):
    """无实际前缀时先用普通求解做"初始确认计划"。"""
    from app.services import run_scenario
    scn = ScenarioIn.model_validate(payload)
    out = run_scenario(scn)
    return out.model_dump()


def _confirmed(payload, plan, version="v1", contract=None):
    return {
        "forecast_version": version,
        "contract": contract or {"notice_steps": 2, "down_cost_multiplier": 10.0,
                                 "emergency": False},
        "result": plan,
    }


def _replan(payload, actual, forecast, *, version="v2", confirmed=None,
            contract=None, ranges=None, revision=None, impute="none"):
    req = ReplanRequest(
        scenario=payload, forecast_version=version,
        forecast=forecast, actual=actual,
        confirmed=confirmed,
        contract=ContractIn.model_validate(contract or
                                           {"notice_steps": 2, "emergency": False}),
        impute_inflow=impute, curve_revision=revision,
        evaluated_scenarios=ranges)
    scn = ScenarioIn.model_validate(payload)
    return replan(req, scn)


# ---------- 1) 已执行前缀固定，重算不改变过去 ----------

def test_actual_prefix_is_fixed(base_payload):
    m = 3
    plan = _first_plan(base_payload)
    actual = [_actual_step(base_payload, t,
                           delivery_override={"life": 30, "industry": 45, "agriculture": 60})
              for t in range(m)]
    fc = _future_forecast(base_payload, m, 30)
    out = _replan(base_payload, actual, fc, confirmed=_confirmed(base_payload, plan))

    p = out["plan"]
    # 前 m 行的供水必须等于实际输入，而非优化值
    for t in range(m):
        row = p["ledger"][t]
        assert row["delivery_m3"]["agriculture"] == pytest.approx(60 * 1e4)
        assert row["delivery_m3"]["life"] == pytest.approx(30 * 1e4)
    # 每个时段仍然守恒（前缀账面 + 未来 LP）
    for row in p["ledger"]:
        lhs = row["start_storage_m3"] + row["inflow_m3"] - row["evaporation_m3"] \
            - row["delivery_total_m3"] - row["spill_m3"]
        assert lhs == pytest.approx(row["end_storage_m3"], abs=1e-2)


def test_missing_actual_inflow_rejected(base_payload):
    plan = _first_plan(base_payload)
    bad = _actual_step(base_payload, 0)
    bad.inflow = q(None, "万m3")
    fc = _future_forecast(base_payload, 1, 30)
    with pytest.raises(Exception):
        _replan(base_payload, [bad], fc, confirmed=_confirmed(base_payload, plan))


# ---------- 2) 预测骤降：归因主要落在 forecast，且通知期承诺硬固定 ----------

def _actual_as_planned(payload, plan, m):
    """构造严格按已确认计划执行的实际前缀（供水/弃水都取计划值）。"""
    out = []
    for t in range(m):
        row = plan["ledger"][t]
        out.append(_actual_step(
            payload, t,
            delivery_override={k: row["delivery_m3"][k] / 1e4
                               for k in ("life", "industry", "agriculture")},
            spill=row["spill_m3"] / 1e4 if row["spill_m3"] else 0.0))
    return out


def test_forecast_plunge_changes_future_not_past(base_payload):
    m = 3
    plan = _first_plan(base_payload)
    actual = _actual_as_planned(base_payload, plan, m)
    fc_dry = _future_forecast(base_payload, m, 30, scale=0.25)  # 预测骤降 75%

    out = _replan(base_payload, actual, fc_dry, version="v2-dry",
                  confirmed=_confirmed(base_payload, plan))
    a = out["attribution"]
    # 执行严格按计划：前缀供水执行差为 0，未来段"起点状态差"也应≈0
    assert sum(abs(v) for v in a["actual_execution_gap_m3"].values()) < 1e-6
    assert abs(sum(a["due_to_execution_m3"].values())) < 1e6
    # 预测改变应解释大部分未来供水变化
    total = sum(a["total_change_m3"].values())
    due_fc = sum(a["due_to_forecast_m3"].values())
    assert total < 0  # 预测变差，总供水下降
    assert due_fc < 0 and abs(due_fc) >= abs(total) * 0.7
    # 通知期（未来前 2 步）承诺供水不可变：等于旧计划
    for t in (m, m + 1):
        for k in ("life", "industry", "agriculture"):
            old_v = plan["ledger"][t]["delivery_m3"][k]
            new_v = out["plan"]["ledger"][t]["delivery_m3"][k]
            assert new_v == pytest.approx(old_v, rel=1e-6)
    # 欠诺/改变代价单独列示
    assert "totals" in out["plan"]
    assert out["plan"]["totals"]["commitment_change_cost"] >= 0


# ---------- 3) 实际取水超计划：归因主要落在 execution，从真实库容起算 ----------

def test_over_delivery_execution_attribution(base_payload):
    m = 3
    plan = _first_plan(base_payload)
    # 前两天多放了农业水（超出旧计划），实测库存因此低于账面推算
    actual = []
    measured_wan = None
    # 先按"实际满放农业"构造，跑一遍账面路径取得账面末库容
    tmp = [_actual_step(base_payload, t,
                        delivery_override={"life": 30, "industry": 45, "agriculture": 120})
           for t in range(m)]
    from app.rolling import build_prefix
    from app.schemas import ScenarioIn
    from app.services import build_curve
    scn = ScenarioIn.model_validate(base_payload)
    pfx = build_prefix(scn, tmp, build_curve(scn))
    measured_wan = max(200, pfx.book_final / 1e4 - 150)  # 比账面少 150 万m³
    actual = [_actual_step(base_payload, t,
                           delivery_override={"life": 30, "industry": 45, "agriculture": 120},
                           measured_storage=measured_wan if t == m - 1 else None)
              for t in range(m)]
    fc = _future_forecast(base_payload, m, 30)
    out = _replan(base_payload, actual, fc, version="v2-over",
                  confirmed=_confirmed(base_payload, plan))
    a = out["attribution"]
    # 实际供水 > 计划供水：执行偏差水量为正，且库存差被记为执行原因（实测低于账面）
    assert sum(a["actual_execution_gap_m3"].values()) > 0
    assert a["current_storage_gap_due_to_execution_m3"] == pytest.approx(-150 * 1e4, rel=1e-3)
    assert len(out["reconciliations"]) >= 1
    assert "实测" in out["reconciliations"][0]["reason"]
    # 未来计划的起点必须是实测库容（经裁剪到边界）
    max_s = out["plan"]["storage_curve"][-1]["storage_m3"]
    assert out["plan"]["ledger"][m - 1]["end_storage_m3"] == pytest.approx(
        min(measured_wan * 1e4, max_s), rel=1e-3)
    # 预测没变：预测归因分量应接近 0
    assert abs(sum(a["due_to_forecast_m3"].values())) < 1e6


# ---------- 4) 库容曲线修订：当前水位不变、库容重估，归因落在 curve ----------

def test_curve_revaluation(base_payload):
    m = 3
    plan = _first_plan(base_payload)
    actual = [_actual_step(base_payload, t) for t in range(m)]
    fc = _future_forecast(base_payload, m, 30)
    # 修订曲线：同一水位对应的库容系统性抬高 8%（测量修正）
    rev_points = []
    for p in base_payload["curve"]:
        rev_points.append({
            "elevation": p["elevation"],
            "storage": {"value": round(p["storage"]["value"] * 1.08, 3),
                        "unit": p["storage"]["unit"]},
            "area": p["area"],
        })
    revision = CurveRevisionIn(
        points=rev_points,
        dead_storage=q(round(base_payload["dead_storage"]["value"] * 1.08, 3), "万m3"),
        max_storage=q(round(base_payload["max_storage"]["value"] * 1.08, 3), "万m3"),
        convert_initial=True)
    out = _replan(base_payload, actual, fc, version="v2-curve",
                  confirmed=_confirmed(base_payload, plan), revision=revision)
    assert out["curve_revision_applied"] is True
    rec = [r for r in out["reconciliations"] if "曲线修订" in r["reason"]]
    assert rec and rec[0]["adjustment_m3"] > 0  # 同水位重估库容抬高
    a = out["attribution"]
    assert a["current_storage_gap_due_to_revaluation_m3"] > 0
    # 修订后仍逐时段守恒
    for row in out["plan"]["ledger"][m:]:
        lhs = row["start_storage_m3"] + row["inflow_m3"] - row["evaporation_m3"] \
            - row["delivery_total_m3"] - row["spill_m3"]
        assert lhs == pytest.approx(row["end_storage_m3"], abs=1e-2)


# ---------- 5) 多来水情景：缺口区间；无概率不输出精确概率 ----------

def test_scenario_ranges_no_fake_probability(base_payload):
    m = 3
    plan = _first_plan(base_payload)
    actual = [_actual_step(base_payload, t) for t in range(m)]
    scenarios = [
        ScenarioForecastIn(key="wet", name="偏丰", probability=None,
                           steps=_future_forecast(base_payload, m, 30, scale=1.3)),
        ScenarioForecastIn(key="dry", name="偏枯", probability=None,
                           steps=_future_forecast(base_payload, m, 30, scale=0.3)),
    ]
    out = _replan(base_payload, actual, scenarios[0].steps, version="v2",
                  confirmed=_confirmed(base_payload, plan), ranges=scenarios)
    assert "不输出期望" in out["probability_note"] or "概率" in out["probability_note"]
    assert len(out["ranges"]) == 2
    dry = next(r for r in out["ranges"] if r["key"] == "dry")
    wet = next(r for r in out["ranges"] if r["key"] == "wet")
    assert dry["probability"] is None and wet["probability"] is None
    # 偏枯情景农业缺口必须大于偏丰
    assert dry["deficit_total_m3"]["agriculture"] >= wet["deficit_total_m3"]["agriculture"]
