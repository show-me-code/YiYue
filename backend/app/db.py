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


def get_session():
    with Session(engine) as session:
        yield session


__all__ = ["engine", "init_db", "get_session", "sqlmodel"]
