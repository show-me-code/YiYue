"""全链路集成测试：上传 → mock LLM 翻译 → 输出（分段增量路径）。"""

import io
import time

from pypdf import PdfReader

from tests.conftest import make_sample_pdf


def _wait_done(client, task_id: int, timeout_s: float = 300) -> dict:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        r = client.get(f"/api/tasks/{task_id}")
        last = r.json()
        if last["status"] in ("done", "failed", "cancelled"):
            return last
        time.sleep(1)
    return last


def test_full_flow_with_parts(client, profile, tmp_path):
    sample = make_sample_pdf(tmp_path / "sample.pdf", pages=3)
    r = client.post("/api/tasks",
                    files={"file": ("sample.pdf", sample.read_bytes(),
                                    "application/pdf")},
                    data={
                        "profile_id": profile["id"], "lang_in": "en",
                        "lang_out": "zh", "part_size": "1",  # 强制分段路径
                    })
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["page_count"] == 3

    final = _wait_done(client, task["id"])
    assert final["status"] == "done", final.get("error")
    kinds = {o["kind"] for o in final["outputs"]}
    assert {"original", "mono", "dual"} <= kinds

    # mono 输出可被解析且页数正确
    mono_url = next(o["url"] for o in final["outputs"] if o["kind"] == "mono")
    pdf_bytes = client.get(mono_url).content
    assert len(PdfReader(sample).pages) == 3
    assert pdf_bytes.startswith(b"%PDF")
    # 必须记录"实际产出这份 PDF 的引擎"，否则回退产出会被误当成源码直译
    assert final["engine_used"] == "babeldoc"

    # Range 请求支持（pdf.js 需要）
    head = client.get(mono_url, headers={"Range": "bytes=0-99"})
    assert head.status_code == 206
    assert head.headers["Content-Range"].startswith("bytes 0-99/")
    assert len(head.content) == 100
    # 同一 URL 下的 PDF 会被重跑替换，必须禁用缓存，否则浏览器一直显示旧译文
    assert "no-store" in head.headers["Cache-Control"]
    assert "no-store" in client.get(mono_url).headers["Cache-Control"]


def test_cache_hit_on_same_options(client, profile, tmp_path):
    sample = make_sample_pdf(tmp_path / "sample2.pdf", pages=1)
    payload = {"file": ("sample2.pdf", sample.read_bytes(),
                        "application/pdf")}
    data = {"profile_id": profile["id"], "part_size": "0"}

    r1 = client.post("/api/tasks", files=payload, data=data)
    t1 = _wait_done(client, r1.json()["id"])
    assert t1["status"] == "done", t1.get("error")

    r2 = client.post("/api/tasks",
                     files={"file": ("sample2.pdf", sample.read_bytes(),
                                     "application/pdf")},
                     data=data)
    t2 = r2.json()
    assert t2["status"] == "done"
    assert t2["cache_hit"] is True
    assert t2["id"] != t1["id"]
    kinds = {o["kind"] for o in t2["outputs"]}
    assert {"original", "mono"} <= kinds


def test_pages_range_selection(client, profile, tmp_path):
    sample = make_sample_pdf(tmp_path / "sample3.pdf", pages=4)
    r = client.post("/api/tasks",
                    files={"file": ("sample3.pdf", sample.read_bytes(),
                                    "application/pdf")},
                    data={"profile_id": profile["id"], "pages": "2-3",
                          "part_size": "0"})
    task = _wait_done(client, r.json()["id"])
    assert task["status"] == "done", task.get("error")
    mono_url = next(o["url"] for o in task["outputs"] if o["kind"] == "mono")
    assert len(PdfReader(io.BytesIO(client.get(mono_url).content)).pages) == 2
