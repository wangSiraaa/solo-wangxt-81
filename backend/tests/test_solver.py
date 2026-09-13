import pytest

from app import demos
from app.core.optimizer import SolveError
from app.schemas import ScenarioIn, SolveOptions
from app.services import ScenarioError, run_scenario


def _opt(**kw):
    return SolveOptions.model_validate(kw)


def test_base_scenario_conservation_and_priority():
    scn = ScenarioIn.model_validate(demos.scenario_base())
    out = run_scenario(scn, _opt())
    assert out.feasible
    # 逐时段守恒残差应≈0（定点迭代+LP 精度）
    for row in out.ledger:
        lhs = (row.start_storage_m3 + row.inflow_m3
               - row.evaporation_m3 - row.delivery_total_m3 - row.spill_m3)
        assert lhs == pytest.approx(row.end_storage_m3, abs=1e-2)
        assert abs(row.conservation_residual_m3) < 1e-2
        assert row.eco_ok  # 生态是硬条件

    t = out.totals
    # 全局水量账闭合
    assert (t["initial_storage"] + t["inflow"]
            == pytest.approx(t["final_storage"] + t["evaporation"]
                             + t["spill"] + sum(v for k, v in t.items()
                                                if k.startswith("delivery_")),
                             rel=1e-6))
    # 生活用水权重最高且有 90% 硬保证：不应有缺口
    assert t["deficit_life"] == pytest.approx(0.0, abs=1e-6)
    # 农业权重最低，枯水期应先于生活/工业承担缺口
    assert t["deficit_agriculture"] >= t["deficit_industry"] - 1e-6


def test_zero_inflow_eventually_conflicts_or_uses_storage():
    scn = ScenarioIn.model_validate(demos.scenario_zero_inflow())
    out = run_scenario(scn, _opt())
    # 零来水：库存+消耗应单调反映（蒸发为正时期末库存下降）
    storages = [row.end_storage_m3 for row in out.ledger]
    # 要么因生态硬约束不可行并指出干旱冲突，要么所有日子靠库存硬撑
    if not out.feasible:
        kinds = {c.kind for c in out.conflicts}
        assert "drought" in kinds
        assert out.conflicts[0].index >= 0
    else:
        assert all(row.eco_ok for row in out.ledger)
        assert storages[-1] <= storages[0] + 1e-6


def test_flood_scenario_flags_flood_period():
    scn = ScenarioIn.model_validate(demos.scenario_flood())
    out = run_scenario(scn, _opt())
    assert not out.feasible
    flood = [c for c in out.conflicts if c.kind == "flood"]
    assert flood, "应识别出洪峰超容"
    # D10(index 9) 是 3200 万m³ 主峰所在
    assert any(c.index in (9, 10) for c in flood)
    for c in flood:
        assert c.gap_m3 > 0


def test_mixed_units_matches_base_totals():
    base = ScenarioIn.model_validate(demos.scenario_base())
    mixed = ScenarioIn.model_validate(demos.scenario_mixed_units())
    a = run_scenario(base, _opt())
    b = run_scenario(mixed, _opt())
    assert b.feasible and a.feasible
    for key in ("inflow", "evaporation", "spill"):
        assert a.totals[key] == pytest.approx(b.totals[key], rel=2e-3)
    assert a.totals["delivery_agriculture"] == pytest.approx(
        b.totals["delivery_agriculture"], rel=2e-3)
    # 水位曲线换算一致
    assert a.level_m[-1] == pytest.approx(b.level_m[-1], abs=1e-3)


def test_missing_inflow_rejected_then_imputed():
    scn = ScenarioIn.model_validate(demos.scenario_missing())
    with pytest.raises(ScenarioError) as ei:
        run_scenario(scn, _opt(impute_inflow="none"))
    assert ei.value.missing_inflow == [4, 5]

    out = run_scenario(scn, _opt(impute_inflow="linear"))
    assert out.feasible
    assert out.imputed_inflow_indexes == [4, 5]
    # 插补行必须打标
    for i in (4, 5):
        assert out.ledger[i].inflow_imputed is True
    # 插补值介于相邻两个实测点之间（D4=120万, D7=90万）
    i4 = out.ledger[4].inflow_m3
    assert out.ledger[6].inflow_m3 - 1 <= i4 <= out.ledger[3].inflow_m3 + 1


def test_weight_override_changes_allocation():
    scn = ScenarioIn.model_validate(demos.scenario_base())
    life_first = run_scenario(scn, _opt(weight_override={
        "life": 3, "industry": 2, "agriculture": 1}))
    agri_first = run_scenario(scn, _opt(weight_override={
        "life": 1, "industry": 1, "agriculture": 5}))
    # 农业优先时，农业供水总量不应小于生活优先口径
    assert (agri_first.totals["delivery_agriculture"]
            >= life_first.totals["delivery_agriculture"] - 1e-6)
    # 生活 90% 硬保证在两种权重下都满足
    for o in (life_first, agri_first):
        delivered = o.totals["delivery_life"]
        demanded = sum(r.demand_m3["life"] for r in o.ledger)
        assert delivered >= 0.9 * demanded - 1e-6


def test_storage_and_eco_are_hard_bounds():
    scn = ScenarioIn.model_validate(demos.scenario_base())
    out = run_scenario(scn, _opt())
    lo = 200 * 1e4
    hi = 2016 * 1e4
    for row in out.ledger:
        assert lo - 1e-6 <= row.end_storage_m3 <= hi + 1e-6
        assert row.end_elevation_m >= 820 - 1e-6
