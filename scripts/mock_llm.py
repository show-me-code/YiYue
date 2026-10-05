"""本地 mock OpenAI 兼容服务（回声式翻译）。

用途：没有 API Key 时体验译阅完整流程。
启动：uv run python ../scripts/mock_llm.py   （或 python scripts/mock_llm.py）
配置档：Base URL 填 http://127.0.0.1:8600/v1，API Key 任意，模型名 mock-model
"""

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat(request: Request):
    payload = await request.json()
    content = payload.get("messages", [{}])[-1].get("content", "")
    text = content.split("\n\n", 1)[-1]  # 回显待译文本，保留占位符结构
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": "mock-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8600, log_level="warning")
