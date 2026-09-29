"""C6 配置与反馈仓储的 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.db.repository.config_feedback import (
    FeedbackRepository,
    RuleRepository,
    RuleUpsert,
)
from app.db.repository.contract import ContractCreate, ContractRepository
from app.generated.schemas import FeedbackVerdict, TargetType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
API_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = API_GATEWAY_ROOT / "alembic.ini"


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


async def _create_schema(dsn: str, schema: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def _drop_schema(dsn: str, schema: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


def _schema_engine(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn,
        connect_args={"server_settings": {"search_path": schema}},
    )


async def _migrate(dsn: str, schema: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    engine = _schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


async def _exercise_config_feedback_repositories(dsn: str, schema: str) -> None:
    engine = _schema_engine(dsn, schema)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            rules = RuleRepository(session)
            rule_id = uuid4()
            created_rule = await rules.upsert(
                RuleUpsert(
                    id=rule_id,
                    rule_name="违约金上限规则",
                    category="PURCHASE",
                    prompt_template="检查违约金是否超过上限。",
                    threshold=0.5,
                    is_enabled=True,
                    review_thresholds={"high": 0.85, "medium": 0.65, "low": 0.40},
                )
            )
            await session.commit()

            assert created_rule.id == rule_id
            assert created_rule.review_thresholds == {"high": 0.85, "medium": 0.65, "low": 0.40}
            assert await rules.get(rule_id) == created_rule
            assert await rules.list(category="PURCHASE") == [created_rule]
            assert await rules.list(category="NDA") == []

            updated_rule = await rules.upsert(
                RuleUpsert(
                    id=rule_id,
                    rule_name="违约金上限规则",
                    category="PURCHASE",
                    prompt_template="更新后的规则模板。",
                    threshold=None,
                    is_enabled=False,
                    review_thresholds={"high": 0.85, "medium": 0.65, "low": 0.40},
                )
            )
            await session.commit()

            assert updated_rule.id == rule_id
            assert updated_rule.prompt_template == "更新后的规则模板。"
            assert updated_rule.threshold is None
            assert updated_rule.is_enabled is False
            assert updated_rule.review_thresholds == {"high": 0.85, "medium": 0.65, "low": 0.40}
            assert await rules.list() == [updated_rule]

            contract_id = uuid4()
            await ContractRepository(session).create(
                ContractCreate(
                    id=contract_id,
                    filename="feedback.docx",
                    file_size=128,
                    file_hash=uuid4().hex,
                    storage_path="feedback/feedback.docx",
                )
            )
            feedbacks = FeedbackRepository(session)
            feedback = await feedbacks.create(
                TargetType.risk,
                "risk-c6-1",
                FeedbackVerdict.correct,
                "风险判断正确。",
                contract_id=contract_id,
                reviewer="法务主管",
            )
            await session.commit()

            records, total = await feedbacks.list()
            assert records == [feedback] and total == 1
            records, total = await feedbacks.list(TargetType.risk)
            assert records == [feedback] and total == 1
            records, total = await feedbacks.list(TargetType.seal)
            assert records == [] and total == 0

            with pytest.raises(ValueError, match="verdict"):
                await feedbacks.create(
                    TargetType.risk,
                    "risk-c6-2",
                    "INVALID_VERDICT",
                    None,
                    contract_id=contract_id,
                )
            with pytest.raises(ValueError, match="target_type"):
                await feedbacks.create(
                    "INVALID_TARGET",
                    "target-c6-1",
                    FeedbackVerdict.correct,
                    None,
                    contract_id=contract_id,
                )
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=(
        "未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 配置/反馈仓储集成测试（保持离线基线）"
    ),
)
def test_config_and_feedback_repositories_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    schema = f"cg_c6_{uuid4().hex}"

    _run(_create_schema(POSTGRES_DSN, schema))
    try:
        _run(_migrate(POSTGRES_DSN, schema))
        _run(_exercise_config_feedback_repositories(POSTGRES_DSN, schema))
    finally:
        _run(_drop_schema(POSTGRES_DSN, schema))
