import importlib


def test_api_auth_and_admin_ui(tmp_path, monkeypatch):
    config = tmp_path / "config"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("TRAFFICGEN_CONFIG_DIR", str(config))
    monkeypatch.setenv("TRAFFICGEN_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("TRAFFICGEN_DATABASE", str(runtime / "traffic.db"))

    module = importlib.import_module("trafficgen.app")
    app = module.create_app()
    app.config.update(TESTING=True)

    client = app.test_client()

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.get_json()["service"] == "netem-traffic-simulator"

    assert client.get("/api/v1/status").status_code == 401

    api_key = (config / "api.key").read_text().strip()
    authorized = client.get(
        "/api/v1/status",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert authorized.status_code == 200

    admin_password = (config / "admin.password").read_text().strip()
    login = client.post("/login", data={"password": admin_password})
    assert login.status_code in (302, 303)

    assert client.get("/").status_code == 200
    assert client.get("/workloads").status_code == 200
    assert client.get("/dem").status_code == 200
    assert client.get("/settings/api").status_code == 200
    assert client.get("/dem/data?minutes=15").status_code == 200

    catalog = client.get(
        "/api/v1/catalog",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert catalog.status_code == 200
    payload = catalog.get_json()
    assert "profiles" in payload
    assert "office" in payload["profiles"]

    module.app.config["TRAFFICGEN_CONTROLLER"].shutdown()
    app.config["TRAFFICGEN_CONTROLLER"].shutdown()
