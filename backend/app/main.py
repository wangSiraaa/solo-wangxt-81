from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select

from . import demos
from .core.units import UnitError, supported
from .db import SchemeRow, ScenarioRow, init_db, session
from .schemas import (CompareOut, LockOut, ScenarioIn, ScenarioMeta,
                      ScenarioOut, SolveOptions, SolveOut)
from .services import ScenarioError, new_id, run_scenario


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(demos.all_builtins)
    yield


app = FastAPI(
    title="教学型水库水量分配模拟",
    version="0.1.0",
    description="逐时段水量账 + LP 配水；教学演示，不替代现实供水决策，不连接真实闸门。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def _parse_scenario(payload: dict[str, Any]) -> ScenarioIn:
    try:
        return ScenarioIn.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors()})


@app.get("/api/health")
async def health():
    return {"ok": True, "units": supported(),
            "notice": "教学模拟，不连接真实闸门，不替代现实供水决策"}


@app.get("/api/demos")
async def list_demos():
    return [{"key": k, "name": v["name"], "description": v.get("description", "")}
            for k, v in demos.all_builtins().items()]


@app.get("/api/demos/{key}")
async def get_demo(key: str):
    d = demos.get_builtin(key)
    if d is None:
        raise HTTPException(404, f"内置场景 {key} 不存在")
    return d


@app.post("/api/validate", response_model=SolveOut)
async def validate(body: dict[str, Any]):
    """不落库：校验单位换算、缺测处理并求解（前端编辑时实时试算）。"""
    payload = body.get("scenario", body)
    options = SolveOptions.model_validate(body.get("options", {}))
    scn = _parse_scenario(payload)
    try:
        return run_scenario(scn, options)
    except ScenarioError as e:
        raise HTTPException(422, detail={
            "message": str(e),
            "missing_inflow": e.missing_inflow,
            "missing_demand": e.missing_demand,
        })
    except UnitError as e:
        raise HTTPException(422, detail={"message": f"单位错误：{e}"})


# ---------- 场景库 ----------

@app.get("/api/scenarios", response_model=list[ScenarioMeta])
async def list_scenarios():
    async with session() as s:
        rows = (await s.execute(select(ScenarioRow).order_by(ScenarioRow.name))).scalars()
        return [ScenarioMeta(id=r.id, name=r.name, description=r.description,
                             builtin=r.builtin == "1") for r in rows]


@app.post("/api/scenarios", response_model=ScenarioMeta)
async def create_scenario(payload: dict[str, Any]):
    scn = _parse_scenario(payload)
    sid = new_id()
    async with session() as s:
        s.add(ScenarioRow(id=sid, name=scn.name, description=scn.description,
                          payload=json.dumps(payload, ensure_ascii=False), builtin="0"))
    return ScenarioMeta(id=sid, name=scn.name, description=scn.description, builtin=False)


@app.get("/api/scenarios/{sid}", response_model=ScenarioOut)
async def get_scenario(sid: str):
    async with session() as s:
        r = await s.get(ScenarioRow, sid)
        if r is None:
            raise HTTPException(404, "场景不存在")
        return ScenarioOut(id=r.id, name=r.name, description=r.description,
                           builtin=r.builtin == "1", payload=json.loads(r.payload))


@app.post("/api/scenarios/{sid}/clone", response_model=ScenarioMeta)
async def clone_scenario(sid: str):
    async with session() as s:
        r = await s.get(ScenarioRow, sid)
        if r is None:
            raise HTTPException(404, "场景不存在")
        payload = json.loads(r.payload)
        payload["name"] = payload.get("name", "") + "（副本）"
        nid = new_id()
        s.add(ScenarioRow(id=nid, name=payload["name"],
                          description=payload.get("description", ""),
                          payload=json.dumps(payload, ensure_ascii=False), builtin="0"))
    return ScenarioMeta(id=nid, name=payload["name"],
                        description=payload.get("description", ""), builtin=False)


# ---------- 方案 ----------

async def _load_scenario(s, sid: str) -> ScenarioIn:
    r = await s.get(ScenarioRow, sid)
    if r is None:
        raise HTTPException(404, "场景不存在")
    return _parse_scenario(json.loads(r.payload))


@app.post("/api/scenarios/{sid}/solve", response_model=SolveOut)
async def solve_scenario(sid: str, options: SolveOptions | None = None,
                         save: bool = True):
    async with session() as s:
        scn = await _load_scenario(s, sid)
        try:
            result = run_scenario(scn, options, scenario_id=sid)
        except ScenarioError as e:
            raise HTTPException(422, detail={"message": str(e),
                                             "missing_inflow": e.missing_inflow})
        if save:
            hid = new_id()
            s.add(SchemeRow(
                id=hid, scenario_id=sid,
                name=options.name if options and options.name else f"方案 {hid[:6]}",
                options=options.model_dump_json() if options else "{}",
                result=json.dumps(result.model_dump(), ensure_ascii=False),
                locked="0"))
            result.scheme_id = hid
        return result


@app.get("/api/scenarios/{sid}/schemes")
async def list_schemes(sid: str):
    async with session() as s:
        rows = (await s.execute(
            select(SchemeRow).where(SchemeRow.scenario_id == sid)
        )).scalars()
        return [{
            "id": r.id, "name": r.name, "locked": r.locked == "1",
            "result": json.loads(r.result),
        } for r in rows]


@app.post("/api/schemes/{hid}/lock", response_model=LockOut)
async def lock_scheme(hid: str, locked: bool = True):
    """锁定一个方案：锁定后不可覆盖，用户可在其旁继续调整并保存另一个方案。"""
    async with session() as s:
        r = await s.get(SchemeRow, hid)
        if r is None:
            raise HTTPException(404, "方案不存在")
        r.locked = "1" if locked else "0"
        return LockOut(scheme_id=hid, locked=locked)


@app.post("/api/scenarios/{sid}/compare", response_model=CompareOut)
async def compare(sid: str, options_list: list[SolveOptions]):
    """一次提交多组选项（如不同权重），返回可并排比较的方案。"""
    if not 2 <= len(options_list) <= 6:
        raise HTTPException(422, "比较需要 2~6 个方案")
    async with session() as s:
        scn = await _load_scenario(s, sid)
        results = []
        for i, opts in enumerate(options_list):
            results.append(run_scenario(scn, opts, scenario_id=sid, name=opts.name or f"方案{i+1}"))
        return CompareOut(scenario_id=sid, schemes=results)


# ---------- 生产态：直接托管构建后的 Angular（开发态用 ng serve + 代理）----------
_WEB_DIST = Path(os.getenv("WEB_DIST", "../frontend/dist/reservoir-frontend/browser"))
if not _WEB_DIST.exists():
    _WEB_DIST = Path(os.getenv("WEB_DIST", "../frontend/dist/reservoir-frontend"))
if _WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")
