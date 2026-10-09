import importlib
import pytest


@pytest.fixture
def simulator(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAFFICGEN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRAFFICGEN_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRAFFICGEN_DATABASE", str(tmp_path / "runtime" / "traffic.db"))
    module = importlib.import_module("trafficgen.app")
    application = module.create_app()
    application.config.update(TESTING=True)
    yield application
    application.config["TRAFFICGEN_CONTROLLER"].shutdown()
