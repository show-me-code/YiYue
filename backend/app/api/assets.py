"""BabelDOC 资产状态与预热。"""

import asyncio
import threading

from fastapi import APIRouter

from app.services import babeldoc_runner

router = APIRouter(prefix="/api/assets", tags=["assets"])

_state = {"warming": False, "ready": None, "message": "尚未检查"}
_lock = threading.Lock()


@router.get("/status")
async def status():
    if _state["ready"] is None and not _state["warming"]:
        # 首次检查：在后台线程加载模型并缓存结果
        def check():
            with _lock:
                if _state["ready"] is not None or _state["warming"]:
                    return
                _state["warming"] = True
            try:
                res = babeldoc_runner.assets_status()
                _state["ready"] = res["ready"]
                _state["message"] = res["message"]
            finally:
                _state["warming"] = False

        asyncio.get_running_loop().run_in_executor(None, check)
    return {"ready": _state["ready"], "warming": _state["warming"],
            "message": _state["message"]}


@router.post("/warmup")
async def warmup():
    if _state["warming"]:
        return {"ok": False, "message": "预热正在进行中"}

    def run():
        with _lock:
            _state["warming"] = True
        try:
            res = babeldoc_runner.warmup_assets()
            _state["ready"] = res["ready"]
            _state["message"] = res["message"]
        except Exception as e:  # noqa: BLE001
            _state["ready"] = False
            _state["message"] = str(e)
        finally:
            _state["warming"] = False

    asyncio.get_running_loop().run_in_executor(None, run)
    return {"ok": True, "message": "预热已开始"}
