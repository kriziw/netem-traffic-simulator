import importlib
import pytest


@pytest.fixture
def simulator(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAFFICGEN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRAFFICGEN_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRAFFICGEN_DATABASE", str(tmp_path / "runtime" / "traffic.db"))
    # Controllers start the DEM worker process; tests start one explicitly where they need it.
    monkeypatch.setenv("TRAFFICGEN_DEM_WORKER", "0")
    module = importlib.import_module("trafficgen.app")
    # Tests start workloads without a lab network; the management-path guard has its own tests.
    monkeypatch.setattr(module, "target_egress", lambda host: None)
    application = module.create_app()
    application.config.update(TESTING=True)
    # Existing operator-view tests exercise the views as delegated controller
    # screens. Read-only boundary tests remove this fixture hook explicitly.
    from flask import g
    def delegated_operator_test():
        g.controller_ui = True
    application.before_request_funcs[None].insert(0, delegated_operator_test)
    yield application
    application.config["TRAFFICGEN_CONTROLLER"].shutdown()
