"""确认前持续检查：基于持久化实际流量/历次确认水位线，而不是当前 active 行。

必须满足：
- 3 条实际的确认之后，提交 2 条 -> 409 拦截；
- 再次提交 2 条 -> 仍然 409（不能因为前一次被拦截而放行）；
- force=true 才放行并留痕；
- 持久化观测把水位线抬高后，同样拦截低条数确认。
"""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import demos
from app.main import app

PLAN_STUB = {"status": "optimal", "feasible": True, "name": "stub",
             "weights": {}, "imputed_inflow_indexes": [], "imputed_demand_indexes": {},
             "impute_method": "none", "evap_iterations": 0, "objective": 0.0,
             "totals": {}, "storage_curve": [], "level_m": [], "ledger": [],
             "conflicts": [], "demand_names": {}}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            yield ac


@pytest.fixture
async def scenario_id(client):
    payload = demos.scenario_base()
    r = await client.post("/api/scenarios", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _confirm(client, sid, version, count, *, force=False, based=None):
    return await client.post(f"/api/scenarios/{sid}/confirm", json={
        "scenario_id": sid, "forecast_version": version, "plan": PLAN_STUB,
        "actual_used_count": count, "force": force,
        "based_on_version": based})


async def test_confirm_watermark_blocks_repeated_stale_submission(client, scenario_id):
    sid = scenario_id
    # 初次 3 条 -> 成功
    r = await _confirm(client, sid, "v1", 3)
    assert r.status_code == 200 and r.json()["blocked"] is False

    # 提交 2 条 -> 拦截
    r1 = await _confirm(client, sid, "v2", 2, based="v1")
    assert r1.status_code == 409
    assert r1.json()["detail"]["watermark_count"] == 3

    # 再次提交 2 条（前一次被拦截，未写入）-> 仍然拦截
    r2 = await _confirm(client, sid, "v2", 2, based="v1")
    assert r2.status_code == 409
    assert r2.json()["detail"]["watermark_count"] == 3

    # active 行仍是 v1（拦截不改变确认状态）
    got = await client.get(f"/api/scenarios/{sid}/confirmation")
    assert got.json()["forecast_version"] == "v1"


async def test_force_confirms_with_trace_and_raises_watermark(client, scenario_id):
    sid = scenario_id
    await _confirm(client, sid, "v1", 3)
    # 强制用 2 条确认 v2
    r = await _confirm(client, sid, "v2", 2, force=True)
    assert r.status_code == 200
    body = r.json()
    assert body["blocked"] is False and "强制确认" in body["warning"]
    # 水位线取历史最大值，仍为 3；现在提交 3 条也低于水位线 3? 不，相等 -> 不拦截
    assert body["watermark_count"] == 3
    r3 = await _confirm(client, sid, "v3", 3, based="v2")
    assert r3.status_code == 200 and r3.json()["blocked"] is False


async def test_persisted_observations_raise_watermark(client, scenario_id):
    sid = scenario_id
    # 只确认过 1 条
    r = await _confirm(client, sid, "v1", 1)
    assert r.status_code == 200
    # 后来上报了 4 个时段的实际流量
    obs = [{"step_index": i, "duration": {"value": 1, "unit": "d"},
            "inflow": {"value": 100, "unit": "万m3"}, "delivery": {}} for i in range(4)]
    r = await client.post(f"/api/scenarios/{sid}/actual-observations", json={"steps": obs})
    assert r.status_code == 200 and r.json()["watermark_count"] == 4
    # 只基于 3 条确认 -> 被持久化观测水位线拦截
    r = await _confirm(client, sid, "v2", 3, based="v1")
    assert r.status_code == 409
    assert r.json()["detail"]["watermark_count"] == 4
    # 重复上报 step 3 不增加去重计数（覆盖而非新增）
    obs_cover = [{"step_index": 3, "duration": {"value": 1, "unit": "d"},
                  "inflow": {"value": 101, "unit": "万m3"}, "delivery": {}}]
    r = await client.post(f"/api/scenarios/{sid}/actual-observations",
                          json={"steps": obs_cover})
    assert r.json()["watermark_count"] == 4


async def test_stale_based_on_version_warns(client, scenario_id):
    sid = scenario_id
    await _confirm(client, sid, "v1", 1)
    await _confirm(client, sid, "v2", 1)
    # 依据已经不是最新的 v1 做确认 -> 版本告警（条数相同不拦截水量原因，但版本原因拦截）
    r = await _confirm(client, sid, "v3", 1, based="v1")
    assert r.status_code == 409
    assert "v1" in r.json()["detail"]["message"]
    assert "v2" in r.json()["detail"]["message"]
