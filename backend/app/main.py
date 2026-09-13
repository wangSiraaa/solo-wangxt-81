from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import Integer, cast, func, select, update

from . import demos, rolling
from .core.units import UnitError, supported
from .db import (ActualObservationRow, PlanConfirmationRow, SchemeRow,
                 ScenarioRow, init_db, session)
from .rolling_schemas import (ActualObservationIn, ActualObservationOut,
                              ConfirmOut, ConfirmRequest, ReplanOut,
                              ReplanRequest)
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


# ---------- 滚动计划 ----------

@app.post("/api/rolling/replan", response_model=ReplanOut)
async def rolling_replan(req: ReplanRequest):
    """滚动重算：已执行前缀固定、承诺按合同规则调整并计改变代价；
    可选多来水情景的缺口区间（无概率不输出概率）。"""
    scn = _parse_scenario(req.scenario)
    try:
        return rolling.replan(req, scn)
    except ScenarioError as e:
        raise HTTPException(422, detail={"message": str(e)})
    except UnitError as e:
        raise HTTPException(422, detail={"message": f"单位错误：{e}"})


@app.get("/api/scenarios/{sid}/confirmation")
async def get_confirmation(sid: str):
    async with session() as s:
        row = (await s.execute(
            select(PlanConfirmationRow)
            .where(PlanConfirmationRow.scenario_id == sid,
                   PlanConfirmationRow.active == "1")
            .order_by(PlanConfirmationRow.created_at.desc())
        )).scalars().first()
        if row is None:
            return None
        return {
            "id": row.id, "scenario_id": row.scenario_id,
            "forecast_version": row.forecast_version,
            "actual_used_count": int(row.actual_used_count or 0),
            "contract": json.loads(row.contract or "{}"),
            "result": json.loads(row.plan),
            "created_at": row.created_at,
        }


async def _watermark(s, sid: str) -> tuple[int, int | None]:
    """实际流量水位线 = max(已持久化观测去重条数, 历次确认实际条数)。

    不依赖当前 active 确认行——即使旧确认被新版本顶替，历史确认依据的
    实际条数与已录入观测仍持续生效。
    """
    obs_n = (await s.execute(
        select(func.count(func.distinct(ActualObservationRow.step_index)))
        .where(ActualObservationRow.scenario_id == sid)
    )).scalar() or 0
    max_step = (await s.execute(
        select(func.max(ActualObservationRow.step_index))
        .where(ActualObservationRow.scenario_id == sid)
    )).scalar()
    confirmed_n = (await s.execute(
        select(func.coalesce(func.max(
            cast(PlanConfirmationRow.actual_used_count, Integer)), 0))
        .where(PlanConfirmationRow.scenario_id == sid)
    )).scalar() or 0
    return max(int(obs_n), int(confirmed_n)), (int(max_step) if max_step is not None else None)


async def _scenario_exists(s, sid: str) -> bool:
    return await s.get(ScenarioRow, sid) is not None


@app.post("/api/scenarios/{sid}/actual-observations", response_model=ActualObservationOut)
async def upsert_observations(sid: str, body: ActualObservationIn):
    """上报/覆盖实际流量观测（按 step_index 去重），作为确认前持续检查的事实水位线。"""
    async with session() as s:
        if not await _scenario_exists(s, sid):
            raise HTTPException(404, "场景不存在")
        for step in body.steps:
            idx = str(int(step["step_index"]))
            existing = (await s.execute(
                select(ActualObservationRow).where(
                    ActualObservationRow.scenario_id == sid,
                    ActualObservationRow.step_index == idx)
            )).scalars().first()
            payload = json.dumps(step, ensure_ascii=False)
            if existing is not None:
                existing.payload = payload
            else:
                s.add(ActualObservationRow(
                    id=new_id(), scenario_id=sid, step_index=idx, payload=payload,
                    created_at=datetime.now(timezone.utc).isoformat()))
        wm, max_step = await _watermark(s, sid)
        return ActualObservationOut(scenario_id=sid, observed_count=wm,
                                    max_step_index=max_step, watermark_count=wm)


@app.post("/api/scenarios/{sid}/confirm", response_model=ConfirmOut)
async def confirm_plan(sid: str, req: ConfirmRequest):
    """确认计划（与预测版本绑定）。

    确认前基于**持久化实际流量水位线**持续检查：本次依据条数低于已知水位线
    （已录入观测或历次确认的最大值）默认 409 拦截、不写入确认；force=true
    可强制放行并标记 forced。历史确认行一律保留，检查不会因 active 行被
    替换而失效——3 条后提交 2 条、再次提交 2 条都会继续被拦截。
    """
    if req.scenario_id != sid:
        raise HTTPException(422, "路径 scenario_id 与请求体不一致")
    async with session() as s:
        if not await _scenario_exists(s, sid):
            raise HTTPException(404, "场景不存在")

        watermark, _ = await _watermark(s, sid)
        prev_versions = [v for (v,) in (await s.execute(
            select(PlanConfirmationRow.forecast_version)
            .where(PlanConfirmationRow.scenario_id == sid)
            .order_by(PlanConfirmationRow.created_at.desc())
        )).all()]

        parts = []
        if req.actual_used_count < watermark:
            parts.append(
                f"已知 {watermark} 个时段的实际流量（持久化观测或历次确认水位线），"
                f"本次计划只基于 {req.actual_used_count} 条——存在更新的实际流量，"
                f"请先拉取实际数据重算后再确认。")
        if req.based_on_version and prev_versions and req.based_on_version != prev_versions[0]:
            parts.append(
                f"本次依据版本 '{req.based_on_version}'，但最新确认版本是 "
                f"'{prev_versions[0]}'——期间预测/承诺可能已再修订。")
        warning = " ".join(parts)

        if warning and not req.force:
            raise HTTPException(409, detail={
                "message": warning,
                "watermark_count": watermark,
                "actual_used_count": req.actual_used_count,
                "previously_confirmed_versions": prev_versions,
                "hint": "如确需在信息不全时确认，请带 force=true 强制确认（会留痕）。",
            })

        await s.execute(
            update(PlanConfirmationRow)
            .where(PlanConfirmationRow.scenario_id == sid,
                   PlanConfirmationRow.active == "1")
            .values(active="0"))
        cid = new_id()
        s.add(PlanConfirmationRow(
            id=cid, scenario_id=sid, forecast_version=req.forecast_version,
            actual_used_count=str(req.actual_used_count),
            contract=json.dumps(req.contract, ensure_ascii=False),
            plan=json.dumps(req.plan, ensure_ascii=False),
            created_at=datetime.now(timezone.utc).isoformat(), active="1",
            forced="1" if warning else "0"))
        return ConfirmOut(
            confirmation_id=cid, scenario_id=sid,
            forecast_version=req.forecast_version,
            actual_used_count=req.actual_used_count,
            watermark_count=max(watermark, req.actual_used_count),
            blocked=False,
            warning=("已在告警下强制确认：" + warning) if warning else "",
            previously_confirmed_versions=prev_versions)


# ---------- 生产态：直接托管构建后的 Angular（开发态用 ng serve + 代理）----------
_WEB_DIST = Path(os.getenv("WEB_DIST", "../frontend/dist/reservoir-frontend/browser"))
if not _WEB_DIST.exists():
    _WEB_DIST = Path(os.getenv("WEB_DIST", "../frontend/dist/reservoir-frontend"))
if _WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")
