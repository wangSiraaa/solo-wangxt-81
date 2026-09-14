"""数据库：默认 PostgreSQL（docker-compose 提供）；
设置 DB_URL=sqlite+aiosqlite:///./dev.db 可零配置本地演示。"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from sqlalchemy import Column, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DB_URL = os.getenv("DB_URL", "postgresql+asyncpg://resuser:respass@localhost:5432/reservoir")


class Base(DeclarativeBase):
    pass


class ScenarioRow(Base):
    __tablename__ = "scenarios"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String, default="")
    payload = Column(Text, nullable=False)          # ScenarioIn JSON
    builtin = Column(String, default="0")           # '1' 内置 / '0' 用户


class SchemeRow(Base):
    __tablename__ = "schemes"
    id = Column(String, primary_key=True)
    scenario_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    options = Column(Text, default="{}")            # SolveOptions JSON
    result = Column(Text, nullable=False)           # SolveOut JSON
    locked = Column(String, default="0")            # 锁定后参数不可改，只能克隆


class PlanConfirmationRow(Base):
    """已确认的滚动计划：与预测版本绑定；下次重算的承诺 C 取自此快照。"""
    __tablename__ = "plan_confirmations"
    id = Column(String, primary_key=True)
    scenario_id = Column(String, nullable=False, index=True)
    forecast_version = Column(String, nullable=False)
    actual_used_count = Column(String, default="0")  # 确认时实际流量条数
    contract = Column(Text, default="{}")
    plan = Column(Text, nullable=False)              # 确认时的 SolveOut JSON
    created_at = Column(String, default="")
    active = Column(String, default="1")             # 每个场景仅一条 active
    forced = Column(String, default="0")             # 是否在告警下强制确认


class ActualObservationRow(Base):
    """持久化的实际流量观测：滚动确认前"是否有新实际流量"的持续检查依据。

    与确认行是否 active 无关——即使旧确认被新版本顶替，已录入的实际流量
    仍然是事实水位线，不能被绕过。
    """
    __tablename__ = "actual_observations"
    id = Column(String, primary_key=True)
    scenario_id = Column(String, nullable=False, index=True)
    step_index = Column(String, nullable=False)      # 场景绝对时段编号
    payload = Column(Text, nullable=False)           # ActualStepIn JSON
    created_at = Column(String, default="")


engine = create_async_engine(DB_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session() -> AsyncSession:
    s = SessionLocal()
    try:
        yield s
        await s.commit()
    except Exception:
        await s.rollback()
        raise
    finally:
        await s.close()


async def init_db(seed_builtins=None) -> None:
    # 先迁移旧表（只补缺列），再建缺失的新表；两步都幂等，每次启动可重复执行
    from .migrations import run_migrations
    await run_migrations(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if seed_builtins:
        from sqlalchemy import select
        async with session() as s:
            for key, payload in seed_builtins().items():
                existing = await s.get(ScenarioRow, f"demo-{key}")
                if existing is None:
                    s.add(ScenarioRow(
                        id=f"demo-{key}", name=payload["name"],
                        description=payload.get("description", ""),
                        payload=json.dumps(payload, ensure_ascii=False), builtin="1"))
