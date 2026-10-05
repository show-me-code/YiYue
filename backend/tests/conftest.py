"""测试夹具：隔离数据目录 + mock OpenAI 兼容服务。

必须在导入 app 包之前设置 YIYUE_DATA_DIR。
"""

import os
import tempfile
import threading
import time
from pathlib import Path

_DATA = tempfile.mkdtemp(prefix="yiyue-test-")
os.environ["YIYUE_DATA_DIR"] = _DATA

import pytest  # noqa: E402
import pymupdf  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def make_sample_pdf(path: str | Path, pages: int = 2) -> Path:
    """生成带真实文本层的样例 PDF（回声翻译可校验）。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(72, 72, 520, 760),
            f"Sample paragraph {i}: The quick brown fox jumps over the lazy dog. "
            * 12,
            fontsize=12,
        )
    doc.save(path)
    doc.close()
    return Path(path)


@pytest.fixture(scope="session")
def mock_llm_base_url():
    """启动一个 OpenAI 兼容 mock 服务（回声式翻译，保留占位符结构）。"""
    app2 = FastAPI()

    @app2.post("/v1/chat/completions")
    async def chat2(request: Request):
        payload = await request.json()
        content = payload.get("messages", [{}])[-1].get("content", "")
        text = content.split("\n\n", 1)[-1]
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10, "completion_tokens": 10,
                "total_tokens": 20,
            },
        }

    config = uvicorn.Config(app2, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def client():
    from app.db import init_db
    from app.main import app

    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def profile(client, mock_llm_base_url):
    resp = client.post("/api/profiles", json={
        "name": "mock", "base_url": mock_llm_base_url,
        "api_key": "test-key", "model": "mock-model",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()
