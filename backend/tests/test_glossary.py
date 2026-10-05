"""术语表服务与 API 测试。"""

from types import SimpleNamespace

from app.services.glossary import entries_to_glossaries


def test_entries_to_glossaries_filter():
    objs = [
        SimpleNamespace(source=" transformer ", target="变换器", tgt_lang="zh",
                        enabled=True),
        SimpleNamespace(source="dropout", target="丢弃法", tgt_lang="zh",
                        enabled=False),
        SimpleNamespace(source="loss", target="损失", tgt_lang="en",
                        enabled=True),
        SimpleNamespace(source="attention", target="注意力", tgt_lang="",
                        enabled=True),
    ]
    gl = entries_to_glossaries(objs, "zh")
    assert len(gl) == 1
    terms = {(e.source, e.target) for e in gl[0].entries}
    assert ("transformer", "变换器") in terms
    assert ("attention", "注意力") in terms
    assert ("dropout", "丢弃法") not in terms
    assert ("loss", "损失") not in terms


def test_empty_returns_empty():
    assert entries_to_glossaries([], "zh") == []


def test_api_crud_and_csv(client):
    r = client.post("/api/glossary", json={
        "source": "transformer", "target": "变换器", "tgt_lang": "zh"})
    assert r.status_code == 200
    eid = r.json()["id"]
    r = client.put(f"/api/glossary/{eid}", json={
        "source": "transformer", "target": "转换器", "tgt_lang": "zh",
        "enabled": True})
    assert r.json()["target"] == "转换器"

    csv_bytes = client.get("/api/glossary/export").content.decode("utf-8-sig")
    assert "transformer" in csv_bytes

    files = {"file": ("g.csv", csv_bytes.encode("utf-8"), "text/csv")}
    r = client.post("/api/glossary/import", files=files)
    assert r.json()["imported"] >= 1

    r = client.delete(f"/api/glossary/{eid}")
    assert r.json()["ok"]

    bad = client.post("/api/glossary/import", files={
        "file": ("g.csv", b"foo,bar\n1,2\n", "text/csv")})
    assert bad.status_code == 400
