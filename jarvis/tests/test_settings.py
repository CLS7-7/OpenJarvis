from conftest import csrf_from


def test_settings_rename_round_trip(client, logged_in):
    page = client.get("/settings").get_data(as_text=True)
    for n in range(1, 6):
        assert f'value="EMPLOYEE_{n}"' in page
    names = {f"name_{i}": name for i, name in enumerate(["Nova", "Rex", "Iris", "Atlas", "Sky <3"], start=1)}
    resp = client.post("/settings", data={"csrf_token": csrf_from(page), **names})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/settings?saved=1")

    page = client.get("/settings?saved=1").get_data(as_text=True)
    assert "Names saved." in page
    assert 'value="Nova"' in page and 'value="Sky &lt;3"' in page

    state = client.get("/api/state").get_json()
    assert [e["name"] for e in state["employees"]] == ["Nova", "Rex", "Iris", "Atlas", "Sky <3"]
    assert [e["key"] for e in state["employees"]] == [f"EMPLOYEE_{n}" for n in range(1, 6)]
    dash = client.get("/").get_data(as_text=True)
    assert '<option value="1">Nova</option>' in dash


def test_settings_rejects_blank_names(client, logged_in):
    page = client.get("/settings").get_data(as_text=True)
    data = {f"name_{i}": "Valid" for i in range(1, 6)}
    data["name_3"] = "   "
    resp = client.post("/settings", data={"csrf_token": csrf_from(page), **data})
    assert resp.status_code == 400
    state = client.get("/api/state").get_json()
    assert [e["name"] for e in state["employees"]] == [f"EMPLOYEE_{n}" for n in range(1, 6)]


def test_settings_never_shows_secrets(client, logged_in):
    page = client.get("/settings").get_data(as_text=True)
    assert "Bridge (Hermes)" in page and "Voice (ElevenLabs)" in page
    assert "Bearer" not in page and "xi-api-key" not in page
