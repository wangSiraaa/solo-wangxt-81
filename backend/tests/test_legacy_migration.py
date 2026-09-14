"""旧库迁移：从只有旧结构 plan_confirmations（无 forced 列）的数据库启动，
迁移必须幂等完成，确认接口在迁移后的表上正常 200。

不依赖 create_all 修改已存在的表——旧表缺列由 app.migrations 补 ALTER ADD COLUMN。
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import db as db_module
from app import demos
from app.main import app

PLAN_STUB = {
    "status": "optimal", "feasible": True, "name": "stub",
    "weights": {}, "imputed_inflow_indexes": [], "imputed_demand_indexes": {},
    "impute_method": "none", "evap_iterations": 0, "objective": 0.0,
    "totals": {}, "storage_curve": [], "level_m": [], "ledger": [],
    "conflicts": [], "demand_names": {},
}

# 旧版 plan_confirmations（没有 forced 列）；scenarios 也按旧结构建空表
_LEGACY_DDL = """
CREATE TABLE scenarios (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  payload TEXT NOT NULL, builtin TEXT DEFAULT '0');
CREATE TABLE plan_confirmations (
  id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, forecast_version TEXT NOT NULL,
  actual_used_count TEXT DEFAULT '0', contract TEXT DEFAULT '{}',
  plan TEXT NOT NULL, created_at TEXT DEFAULT '', active TEXT DEFAULT '1');
"""


@pytest.fixture
async def legacy_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink()  # mkstemp 会建空文件，sqlite 需要自己建表
    con = sqlite3.connect(path)
    try:
        con.executescript(_LEGACY_DDL)
        # 一条旧版本遗留的确认行：没有 forced 值；active=0 不影响当前确认
        con.execute(
            "INSERT INTO plan_confirmations "
            "(id, scenario_id, forecast_version, actual_used_count, contract, "
            " plan, created_at, active) VALUES (?,?,?,?,?,?,?,?)",
            ("legacyrow", "demo-base", "legacy-v1", "3", "{}",
             json.dumps(PLAN_STUB), "2026-09-01T00:00:00+00:00", "0"),
        )
        con.commit()
    finally:
        con.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", maker)
    yield path
    await engine.dispose()
    Path(path).unlink(missing_ok=True)


@pytest.fixture
async def client(legacy_db):
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            yield ac


async def test_legacy_schema_migrates_and_confirm_returns_200(client):
    # 启动迁移后内置场景已播种
    demos_list = await client.get("/api/demos")
    assert demos_list.status_code == 200

    # 1) 旧行的 forced 列被回填，且仍计入水位线（actual_used_count=3）
    got = await client.get("/api/scenarios/demo-base/confirmation")
    # 旧行 active=0 -> 无活跃确认
    assert got.json() is None

    body = {"scenario_id": "demo-base", "forecast_version": "v-new",
            "plan": PLAN_STUB, "actual_used_count": 3}
    r = await client.post("/api/scenarios/demo-base/confirm", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] is False
    assert r.json()["watermark_count"] == 3

    # 2) 迁移后的表上：3 条已确认，再提交 2 条 -> 409；再次 2 条 -> 仍 409
    for _ in range(2):
        r = await client.post("/api/scenarios/demo-base/confirm",
                              json={"scenario_id": "demo-base",
                                    "forecast_version": "v-stale",
                                    "plan": PLAN_STUB,
                                    "actual_used_count": 2,
                                    "based_on_version": "v-new"})
        assert r.status_code == 409
        assert r.json()["detail"]["watermark_count"] == 3

    # 3) 新建场景并确认一次 -> 200（走迁移后含 forced 列的 INSERT 路径）
    payload = demos.scenario_zero_inflow()
    r = await client.post("/api/scenarios", json=payload)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(f"/api/scenarios/{sid}/confirm", json={
        "scenario_id": sid, "forecast_version": "c1",
        "plan": PLAN_STUB, "actual_used_count": 1})
    assert r.status_code == 200, r.text

    # 4) 新表 actual_observations 由 create_all 补建，上报可用
    obs = [{"step_index": 0, "duration": {"value": 1, "unit": "d"},
            "inflow": {"value": 0, "unit": "万m3"}, "delivery": {}}]
    r = await client.post(f"/api/scenarios/{sid}/actual-observations",
                          json={"steps": obs})
    assert r.status_code == 200 and r.json()["watermark_count"] == 1


async def test_migration_is_idempotent(legacy_db):
    """重复执行迁移不报错，第二次起不再产生 ALTER。"""
    from app.migrations import run_migrations
    engine = db_module.engine
    first = await run_migrations(engine)
    assert any("forced" in stmt for stmt in first)
    second = await run_migrations(engine)
    assert second == []
    third = await run_migrations(engine)
    assert third == []
