"""数据库引擎与会话。"""

import sqlmodel
from sqlmodel import Session, SQLModel, create_engine

from app import config

engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    import app.models  # noqa: F401 确保表定义已注册

    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """轻量迁移：为旧库补充新增列（SQLModel create_all 不会改已有表）。"""
    import sqlalchemy

    insp = sqlalchemy.inspect(engine)
    cols = {c["name"] for c in insp.get_columns("profile")}
    if "protocol" not in cols:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE profile ADD COLUMN protocol VARCHAR "
                "DEFAULT 'openai' NOT NULL"
            )
    tcols = {c["name"] for c in insp.get_columns("task")}
    if "engine" not in tcols:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE task ADD COLUMN engine VARCHAR DEFAULT 'auto' NOT NULL"
            )
    if "arxiv_id" not in tcols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN arxiv_id VARCHAR")
    if "engine_used" not in tcols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN engine_used VARCHAR")
            # 旧行回填：带"回退"备注的说明实际是 BabelDOC 产出；其余 arXiv 任务按 arxiv 记
            conn.exec_driver_sql(
                "UPDATE task SET engine_used='babeldoc' "
                "WHERE engine_used IS NULL AND error LIKE '%回退%'")
            conn.exec_driver_sql(
                "UPDATE task SET engine_used='arxiv' "
                "WHERE engine_used IS NULL AND arxiv_id IS NOT NULL")


def get_session():
    with Session(engine) as session:
        yield session


__all__ = ["engine", "init_db", "get_session", "sqlmodel"]
