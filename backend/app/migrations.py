"""轻量级、可重复执行的启动迁移（不依赖 Alembic，也不靠 create_all 改旧表）。

背景：SQLAlchemy `MetaData.create_all(checkfirst=True)` 只会创建**不存在的表**，
不会给已存在的表新增列。旧库的 plan_confirmations 没有 forced 列时，
确认接口插入新行会直接 500（column does not exist）。

策略（幂等，每次启动都可安全执行）：
  1. 用 inspector 读取现有表/列；
  2. 已存在的表若缺列 -> ALTER TABLE ADD COLUMN（带常量 DEFAULT，旧行自动回填）；
  3. 仍不存在的表 -> 由随后的 create_all 建立。

PostgreSQL 11+ 对"常量默认值的 ADD COLUMN"只改元数据、立即回填，不重写表；
SQLite 同样支持。TEXT 在两种方言上都可用。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# 表名 -> [(列名, 列 DDL)]。新增字段时在这里登记一行即可。
_ADDITIONS: dict[str, list[tuple[str, str]]] = {
    "plan_confirmations": [
        ("forced", "TEXT DEFAULT '0'"),   # 是否在告警下强制确认（旧行视为非强制）
    ],
}


def _inspect_sync(conn) -> dict[str, set[str]]:
    from sqlalchemy import inspect
    insp = inspect(conn)
    existing: dict[str, set[str]] = {}
    for table in _ADDITIONS:
        if insp.has_table(table):
            existing[table] = {col["name"] for col in insp.get_columns(table)}
    return existing


async def run_migrations(engine: AsyncEngine) -> list[str]:
    """执行迁移，返回本次实际执行的 ALTER 语句描述（空列表=已是最新）。"""
    applied: list[str] = []
    async with engine.begin() as conn:
        present = await conn.run_sync(_inspect_sync)
        for table, cols in _ADDITIONS.items():
            have = present.get(table)
            if have is None:
                # 整表缺失：交给 create_all 建最新结构，无需 ALTER
                continue
            for name, ddl in cols:
                if name in have:
                    continue
                stmt = f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}'
                await conn.execute(text(stmt))
                applied.append(stmt)
    return applied
