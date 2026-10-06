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


def get_session():
    with Session(engine) as session:
        yield session


__all__ = ["engine", "init_db", "get_session", "sqlmodel"]
