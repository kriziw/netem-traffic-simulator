from __future__ import annotations

import atexit
import json
import copy
import os
import hmac
import secrets
from urllib.parse import urlsplit
import time

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import __version__
from .auth import admin_password_valid, admin_required, bearer_required
from .config import (
    ensure_admin_password,
    ensure_api_key,
    ensure_session_secret,
    load_settings,
    rotate_api_key,
    _write_secret,
)
from .database import init_db, query_dem_timeseries, recent_runs
from .dem import dem_summary
from .engine import WorkloadController
from .profiles import profile_payload
from . import maintenance
from .network import discover, validate_route, ip_json, VENDORS, route_status, validate_interface


def create_app():
    settings = load_settings()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.database_path)
    ensure_api_key(settings)
    ensure_admin_password(settings)

    app = Flask(__name__)
    app.secret_key = ensure_session_secret(settings)
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=1024 * 1024)
    app.config["TRAFFICGEN_SETTINGS"] = settings
    app.config["TRAFFICGEN_CONTROLLER"] = WorkloadController(settings)

    @app.before_request
    def protect_ui_forms():
        if request.method == "POST" and not request.path.startswith("/api/v1/"):
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                return {"error": "Invalid form token. Reload the page and retry."}, 400

    @app.after_request
    def private_responses(response):
        if request.path != "/api/v1/health":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.context_processor
    def inject_global():
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return {
            "csrf_token": session["csrf_token"],
            "app_version": __version__,
            "instance_name": settings.instance_name,
            "controller_status": app.config["TRAFFICGEN_CONTROLLER"].status(),
        }

    def host_available(require_idle=False):
        if maintenance.status(settings)["busy"]:
            raise ValueError("Wait for the pending update or routing task to finish.")
        if require_idle and app.config["TRAFFICGEN_CONTROLLER"].status().get("status") in ("starting", "running", "stopping"):
            raise ValueError("Stop the workload before changing appliance routing or installing an update.")

    def routed_payload(payload):
        host_available()
        selected = maintenance.status(settings).get("selected")
        if selected:
            try:
                actual = ip_json("route", "get", selected["target"])[0]
            except (OSError, ValueError, IndexError) as exc:
                raise ValueError("Cannot verify the selected appliance route. Verify & select it again.") from exc
            if actual.get("dev") != selected["interface"] or actual.get("gateway") != selected["gateway"] or 'linkdown' in actual.get('flags', []):
                raise ValueError("The selected appliance route is missing or changed. Verify & select it again.")
            payload.setdefault("target", f"http://{selected['target']}:8090")
            if urlsplit(str(payload.get("target", ""))).hostname != selected["target"]:
                raise ValueError("Workload target must match the selected appliance's benchmark route.")
        return payload

    def saved_appliances():
        rows = maintenance.read_json(settings.runtime_dir / "appliances.json", [])
        return rows if isinstance(rows, list) else []

    def system_snapshot():
        state = maintenance.status(settings)
        try:
            policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
            state["network"] = discover(policy.get("management_interface", "eth0"))
            state["route_health"] = route_status(state.get("selected"), policy.get("management_interface", "eth0"), state["network"]["interfaces"])
            scanned = maintenance.read_json(maintenance.ADMIN_DIR / "discovery.json", {})
            state["scanned_candidates"] = scanned.get("candidates", [])
        except (OSError, ValueError) as exc:
            state["network"] = {"interfaces": [], "candidates": [], "error": str(exc)}
            state["route_health"] = {"state": "error", "active": False, "message": "Cannot inspect live network state: " + str(exc)}
        state["appliances"] = saved_appliances()
        return state

    @app.get("/settings/system")
    @admin_required
    def system_settings():
        return render_template("system.html", page="system", system=system_snapshot(), vendors=VENDORS)

    @app.get("/settings/system/data")
    @admin_required
    def system_data():
        return jsonify(system_snapshot())

    @app.post("/settings/system/action")
    @admin_required
    def system_action():
        action = request.form.get("action", "")
        try:
            host_available(require_idle=action in ("route", "clear_route", "install_update", "configure_interface", "forget_interface"))
            if action == "save_appliance":
                policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
                route = validate_route(dict(request.form), policy.get("management_interface", "eth0"))
                name = request.form.get("name", "").strip()[:80]
                vendor = request.form.get("vendor", "Other")
                if not name or vendor not in VENDORS:
                    raise ValueError("Provide an appliance name and supported vendor label.")
                rows = saved_appliances()
                if len(rows) >= 20:
                    raise ValueError("Up to 20 appliances can be saved.")
                rows.append({**route, "name": name, "vendor": vendor, "id": secrets.token_hex(8)})
                _write_secret(settings.runtime_dir / "appliances.json", json.dumps(rows))
                flash("Appliance saved. Select it below to verify and apply its traffic route.", "success")
            elif action == "delete_appliance":
                rows = [row for row in saved_appliances() if row["id"] != request.form.get("appliance_id")]
                _write_secret(settings.runtime_dir / "appliances.json", json.dumps(rows))
                flash("Saved appliance removed. Any active route remains until cleared or changed.", "info")
            else:
                payload = {}
                if action == "route":
                    payload = next((row for row in saved_appliances() if row["id"] == request.form.get("appliance_id")), None)
                    if payload is None:
                        raise ValueError("Choose a saved appliance.")
                elif action == "configure_interface":
                    policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
                    payload = validate_interface(dict(request.form), policy.get("management_interface", "eth0"))
                elif action == "forget_interface":
                    payload = {"interface": request.form.get("interface", "")}
                elif action == "install_update":
                    payload = {"tag": request.form.get("tag", "")}
                maintenance.enqueue(settings, action, payload)
                flash("Task queued. Its result will appear below without refreshing the page.", "info")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("system_settings"))

    @app.get("/login")
    def login():
        if session.get("trafficgen_admin"):
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        password = request.form.get("password", "")
        if admin_password_valid(password):
            session.clear()
            session["trafficgen_admin"] = True
            target = request.args.get("next") or request.form.get("next")
            parsed = urlsplit(target or "")
            if not target or not target.startswith("/") or target.startswith("//") or parsed.netloc or "\\" in target:
                target = url_for("dashboard")
            return redirect(target)
        flash("Invalid administrator password.", "error")
        return render_template("login.html"), 401

    @app.post("/logout")
    @admin_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @admin_required
    def dashboard():
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        return render_template(
            "dashboard.html",
            page="dashboard",
            status=status,
            profiles=profile_payload(),
            runs=recent_runs(settings.database_path, 8),
        )

    @app.get("/workloads")
    @admin_required
    def workloads():
        return render_template(
            "workloads.html",
            page="workloads",
            status=app.config["TRAFFICGEN_CONTROLLER"].status(),
            catalog=profile_payload(),
            config_default_target=(f"http://{maintenance.status(settings)['selected']['target']}:8090" if maintenance.status(settings)["selected"] else settings.default_target),
        )

    @app.post("/workloads/start")
    @admin_required
    def workload_start_ui():
        payload = {
            "profile": request.form.get("profile"),
            "users": request.form.get("users"),
            "spawn_rate": request.form.get("spawn_rate"),
            "activity": request.form.get("activity"),
            "pattern": request.form.get("pattern"),
            "target": request.form.get("target"),
        }
        try:
            app.config["TRAFFICGEN_CONTROLLER"].start(routed_payload(payload))
            flash("Corporate workload started.", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("workloads"))

    @app.post("/workloads/adjust")
    @admin_required
    def workload_adjust_ui():
        payload = {
            "users": request.form.get("users"),
            "spawn_rate": request.form.get("spawn_rate"),
            "activity": request.form.get("activity"),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, "")}
        try:
            app.config["TRAFFICGEN_CONTROLLER"].adjust(payload)
            flash("Workload adjusted.", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("workloads"))

    @app.post("/workloads/stop")
    @admin_required
    def workload_stop_ui():
        app.config["TRAFFICGEN_CONTROLLER"].stop()
        flash("Workload stopped.", "info")
        return redirect(url_for("workloads"))

    @app.get("/dem")
    @admin_required
    def dem_page():
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        return render_template(
            "dem.html",
            page="dem",
            status=status,
            summary=dem_summary(settings.database_path, run_id=run_id, window_seconds=60),
            runs=recent_runs(settings.database_path, 12),
        )

    @app.get("/ui/status")
    @admin_required
    def ui_status():
        return jsonify(app.config["TRAFFICGEN_CONTROLLER"].status())

    @app.get("/dem/data")
    @admin_required
    def dem_data():
        try:
            minutes = max(1, min(1440, int(request.args.get("minutes", "15"))))
        except ValueError:
            return {"error": "minutes must be an integer."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        return jsonify(
            {
                "timestamp": time.time(),
                "users": status.get("users", 0),
                "summary": dem_summary(
                    settings.database_path,
                    run_id=run_id,
                    window_seconds=min(3600, minutes * 60),
                ),
                "samples": query_dem_timeseries(
                    settings.database_path,
                    time.time() - minutes * 60,
                    limit=3000,
                ),
            }
        )

    @app.get("/settings/api")
    @admin_required
    def api_settings():
        return render_template(
            "api.html",
            page="api",
            api_key=ensure_api_key(settings),
            base_url=f"https://HOST:{settings.api_port}/api/v1",
            api_port=settings.api_port,
            discovery_port=settings.discovery_port,
        )

    @app.post("/settings/api/rotate")
    @admin_required
    def api_rotate():
        key = rotate_api_key(settings)
        flash(
            "API key rotated. Existing integrations stop working immediately. "
            "Update NetEm with the new key.",
            "warning",
        )
        return render_template(
            "api.html",
            page="api",
            api_key=key,
            base_url=f"https://HOST:{settings.api_port}/api/v1",
            api_port=settings.api_port,
            discovery_port=settings.discovery_port,
            rotated=True,
        )

    @app.get("/api/v1/network")
    @bearer_required
    def api_network():
        return jsonify(system_snapshot())

    @app.post("/api/v1/network/select")
    @bearer_required
    def api_network_select():
        try:
            host_available(require_idle=True)
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict):
                raise ValueError("Request must be an object.")
            selected = next((row for row in saved_appliances() if row["id"] == data.get("appliance_id")), None)
            if selected is None:
                raise ValueError("Choose a saved appliance_id.")
            return jsonify(maintenance.enqueue(settings, "route", selected)), 202
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    # ----- Versioned integration API -----

    @app.get("/api/v1/health")
    def api_health():
        return {
            "service": "netem-traffic-simulator",
            "version": __version__,
            "api_version": "v1",
            "status": "ok",
            "instance_name": settings.instance_name,
        }

    @app.get("/api/v1/status")
    @bearer_required
    def api_status():
        return jsonify(app.config["TRAFFICGEN_CONTROLLER"].status())

    @app.get("/api/v1/catalog")
    @bearer_required
    def api_catalog():
        return jsonify(profile_payload())

    @app.get("/api/v1/profiles")
    @bearer_required
    def api_profiles():
        return jsonify({"profiles": profile_payload()["profiles"]})

    @app.get("/api/v1/applications")
    @bearer_required
    def api_applications():
        return jsonify({"applications": profile_payload()["applications"]})

    def workload_payload():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    @app.post("/api/v1/workloads/start")
    @bearer_required
    def api_workload_start():
        try:
            status = app.config["TRAFFICGEN_CONTROLLER"].start(
                routed_payload(workload_payload())
            )
            return jsonify(status), 201
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except RuntimeError as exc:
            return {"error": str(exc)}, 409

    @app.post("/api/v1/workloads/adjust")
    @bearer_required
    def api_workload_adjust():
        try:
            return jsonify(
                app.config["TRAFFICGEN_CONTROLLER"].adjust(
                    workload_payload()
                )
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except RuntimeError as exc:
            return {"error": str(exc)}, 409

    @app.post("/api/v1/workloads/stop")
    @bearer_required
    def api_workload_stop():
        return jsonify(app.config["TRAFFICGEN_CONTROLLER"].stop())

    @app.get("/api/v1/dem/summary")
    @bearer_required
    def api_dem_summary():
        try:
            window = max(10, min(3600, int(request.args.get("window", "60"))))
        except ValueError:
            return {"error": "window must be an integer from 10 to 3600 seconds."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        summary = dem_summary(
            settings.database_path,
            run_id=run_id,
            window_seconds=window,
        )
        summary["active_users"] = status.get("users", 0)
        summary["workload_status"] = status.get("status", "idle")
        summary["run_id"] = run_id
        return jsonify(summary)

    @app.get("/api/v1/dem/applications")
    @bearer_required
    def api_dem_applications():
        try:
            window = max(10, min(3600, int(request.args.get("window", "60"))))
        except ValueError:
            return {"error": "window must be an integer."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window)
        return jsonify(
            {
                "timestamp": summary["timestamp"],
                "window_seconds": window,
                "run_id": run_id,
                "applications": summary["applications"],
            }
        )

    @app.get("/api/v1/dem/personas")
    @bearer_required
    def api_dem_personas():
        try:
            window = max(10, min(3600, int(request.args.get("window", "60"))))
        except ValueError:
            return {"error": "window must be an integer."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window)
        return jsonify(
            {
                "timestamp": summary["timestamp"],
                "window_seconds": window,
                "run_id": run_id,
                "personas": summary["personas"],
            }
        )

    @app.get("/api/v1/dem/endpoints")
    @bearer_required
    def api_dem_endpoints():
        try:
            window = max(10, min(3600, int(request.args.get("window", "60"))))
            limit = max(1, min(1000, int(request.args.get("limit", "200"))))
        except ValueError:
            return {"error": "window/limit must be integers."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window)
        endpoints = list(summary["endpoints"].values())
        endpoints.sort(
            key=lambda item: (
                item["experience_score"] is None,
                item["experience_score"] if item["experience_score"] is not None else 999,
            )
        )
        return jsonify(
            {
                "timestamp": summary["timestamp"],
                "window_seconds": window,
                "run_id": run_id,
                "endpoints": endpoints[:limit],
            }
        )

    @app.get("/api/v1/dem/timeseries")
    @bearer_required
    def api_dem_timeseries():
        try:
            minutes = max(1, min(1440, int(request.args.get("minutes", "15"))))
        except ValueError:
            return {"error": "minutes must be an integer from 1 to 1440."}, 400
        samples = query_dem_timeseries(
            settings.database_path,
            time.time() - minutes * 60,
            limit=3000,
        )
        return jsonify(
            {
                "timestamp": time.time(),
                "minutes": minutes,
                "samples": samples,
            }
        )

    @app.get("/api/v1/dem/experience")
    @bearer_required
    def api_dem_experience():
        # Purpose-built DEM endpoint for NetEm and external integrations.
        try:
            window = max(10, min(3600, int(request.args.get("window", "60"))))
        except ValueError:
            return {"error": "window must be an integer."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window)
        return jsonify(
            {
                "timestamp": summary["timestamp"],
                "run_id": run_id,
                "workload_status": status.get("status", "idle"),
                "active_users": status.get("users", 0),
                "window_seconds": summary["window_seconds"],
                "requests": summary["requests"],
                "truncated": summary["truncated"],
                "endpoint_experience": {
                    "score": summary["experience_score"],
                    "rating": summary["experience"],
                    "availability_pct": summary["availability_pct"],
                    "p50_ms": summary["p50_ms"],
                    "p95_ms": summary["p95_ms"],
                    "requests_per_second": summary["requests_per_second"],
                    "failures_per_second": summary["failures_per_second"],
                },
                "applications": summary["applications"],
                "personas": summary["personas"],
                "endpoints": sorted(
                    summary["endpoints"].values(),
                    key=lambda item: (
                        item["experience_score"] is None,
                        item["experience_score"] if item["experience_score"] is not None else 999,
                    ),
                )[:50],
            }
        )

    @app.get("/api/v1/runs")
    @bearer_required
    def api_runs():
        return jsonify({"runs": recent_runs(settings.database_path, 50)})

    atexit.register(app.config["TRAFFICGEN_CONTROLLER"].shutdown)
    return app


app = create_app()
