"""译阅后端入口。"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import assets, files, glossary, profiles, tasks
from app.db import init_db
from app.services.babeldoc_runner import ensure_init
from app.services.orchestrator import manager, recover_interrupted

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# BabelDOC/onnx 日志降噪
for noisy in ("httpx", "httpcore", "babeldoc.assets.assets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recover_interrupted()
    ensure_init()
    await manager.start()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="译阅 YiYue", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in (profiles.router, glossary.router, tasks.router, files.router,
              assets.router):
        app.include_router(r)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "yiyue"}

    # 生产模式：挂载前端构建产物
    dist = config.ROOT.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
