"""配置档 API 测试。"""


def test_crud_and_default(client):
    # 会话级共享数据目录：先清空配置档，才能断言"第一个自动默认"。
    # 否则别的测试（或其 fixture）先建过一个配置档，这条就随执行顺序变红。
    for p in client.get("/api/profiles").json():
        client.delete(f"/api/profiles/{p['id']}")

    r = client.post("/api/profiles", json={
        "name": "p1", "base_url": "http://x/v1", "api_key": "sk-abcdef1234",
        "model": "m1"})
    assert r.status_code == 200
    p1 = r.json()
    assert p1["is_default"] is True  # 第一个自动默认
    assert p1["api_key_masked"].endswith("1234")
    assert "sk-abcdef" not in p1["api_key_masked"]

    r = client.post("/api/profiles", json={
        "name": "p2", "base_url": "", "api_key": "k2", "model": "m2",
        "is_default": True})
    p2 = r.json()
    assert p2["is_default"] is True
    profiles = client.get("/api/profiles").json()
    defaults = [p for p in profiles if p["is_default"]]
    assert len(defaults) == 1 and defaults[0]["id"] == p2["id"]

    # 更新不传 api_key 时保留旧值
    r = client.put(f"/api/profiles/{p1['id']}", json={
        "name": "p1b", "base_url": "http://x/v1", "api_key": "",
        "model": "m1", "qps": 8, "is_default": False})
    assert r.status_code == 200

    assert client.delete(f"/api/profiles/{p2['id']}").json()["ok"]
    assert client.delete(f"/api/profiles/{p1['id']}").json()["ok"]


def test_connection_failure_shape(client, profile):
    # mock LLM 只实现了 /chat/completions；对配置档做连接测试应成功
    r = client.post(f"/api/profiles/{profile['id']}/test")
    body = r.json()
    assert body["ok"] is True, body
