from __future__ import annotations

import atexit
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
)
from .database import init_db, query_dem_timeseries, recent_runs
from .dem import dem_summary
from .engine import WorkloadController
from .profiles import profile_payload


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
            config_default_target=settings.default_target,
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
            app.config["TRAFFICGEN_CONTROLLER"].start(payload)
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
                workload_payload()
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
