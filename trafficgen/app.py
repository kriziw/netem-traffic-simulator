from __future__ import annotations

import atexit
import json
import copy
import os
import hmac
import secrets
import subprocess
import re
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import time

from flask import (
    Flask,
    flash,
    g,
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
from .experience_policy import load_policy, save_policy
from .diagnosis import target_finding
from .engine import WorkloadController
from .profiles import profile_payload
from . import maintenance
from . import remote_target
from . import traffic_paths
from . import network_setup
from . import setup_probe
from . import data_dhcp
from .appliance_identity import enrich_candidates
from .proxmox_inventory import validate_config
from .network import discover, validate_route, ip_json, VENDORS, route_status, validate_interface, interfaces, path_readiness, address_table


def target_egress(host):
    """The kernel's route to a workload target, or None when it cannot be looked up."""
    try:
        return ip_json("route", "get", host)[0]
    except (OSError, ValueError, IndexError):
        return None


# The target's clock offset from this simulator, from its last health check (positive: ahead).
TARGET_CLOCK = {"offset_s": None, "checked": None}
HOST_CLOCK = {"value": None, "checked": None}


def host_clock():
    """This host's NTP state, read at most once a minute. A container shares its host's clock."""
    if HOST_CLOCK["checked"] is not None and time.monotonic() - HOST_CLOCK["checked"] < 60:
        return HOST_CLOCK["value"]
    value = {"ntp": None, "synchronized": None, "container": None}
    try:
        shown = subprocess.run(["timedatectl", "show", "--property=NTP", "--property=NTPSynchronized"],
                               capture_output=True, text=True, timeout=5, check=False)
        flags = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line) if shown.returncode == 0 else {}
        value.update(ntp=flags["NTP"] == "yes" if "NTP" in flags else None,
                     synchronized=flags["NTPSynchronized"] == "yes" if "NTPSynchronized" in flags else None)
        virt = subprocess.run(["systemd-detect-virt", "--container"], capture_output=True, text=True, timeout=5, check=False)
        value["container"] = virt.stdout.strip() not in ("", "none")
    except (OSError, subprocess.SubprocessError):
        pass
    HOST_CLOCK.update(value=value, checked=time.monotonic())
    return value


def clock_report():
    """What NetEm needs to compare clocks: this host's time sync and the target's recent offset."""
    recent = TARGET_CLOCK["checked"] is not None and time.monotonic() - TARGET_CLOCK["checked"] < 900
    return dict(host_clock(), target_offset_s=TARGET_CLOCK["offset_s"] if recent else None)


def target_health(host):
    """Ask the controlled target's health endpoint over the current route, never through a proxy."""
    sent = time.time()
    try:
        with build_opener(ProxyHandler({})).open(f"http://{host}:8090/health", timeout=3) as response:
            body = json.loads(response.read(4096) or b"{}")
    except (OSError, ValueError) as exc:
        return False, f"The controlled target at {host} did not answer ({exc})."
    received = time.time()
    if not isinstance(body, dict) or body.get("service") != "netem-traffic-target":
        return False, f"{host} answered, but not as the controlled target."
    if isinstance(body.get("time"), (int, float)):
        TARGET_CLOCK.update(offset_s=round(body["time"] - (sent + received) / 2, 2), checked=time.monotonic())
    return True, f"The controlled target at {host} answers through this path."


def path_watchdog(application, stop_event, first=15, interval=30, backoff=120, log=print):
    """Restore a saved appliance route that disappeared (reboot, link flap) without an operator.
    It only repairs a route the operator selected; it never picks an appliance by itself."""
    last, delay, reported = None, first, None
    while not stop_event.wait(delay):
        delay = interval
        try:
            readiness = application.config["TRAFFICGEN_NETWORK_READINESS"](check_target=False)
            if application.config.get("TRAFFICGEN_PATH_HEALTH"):
                application.config["TRAFFICGEN_PATH_HEALTH"]()
            due = last is None or time.monotonic() - last >= backoff
            if (not readiness["ready"] and readiness["repairable"] and readiness.get("saved_route")
                    and not readiness.get("busy") and due):
                last = time.monotonic()
                application.config["TRAFFICGEN_REPAIR"]()
                log("Traffic path lost; automatic repair queued: " + readiness["message"])
            reported = None
        except Exception as exc:
            if str(exc) != reported:
                reported = str(exc)
                log("Traffic path watchdog: " + reported)


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

    @app.before_request
    def execution_monitor_only():
        if getattr(g, 'controller_ui', False) or request.path.startswith('/api/v1/'):
            return None
        if request.method == 'POST' and request.endpoint not in ('login_post', 'logout'):
            return {'error': 'This execution agent is read-only. Use NetEm to issue commands.'}, 403
        if request.method == 'GET' and request.path in (
                '/workloads', '/traffic-paths', '/settings/network-setup', '/settings/system', '/settings/api'):
            return redirect(url_for('dashboard'))

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
            "controller_ui": bool(getattr(g, 'controller_ui', False)),
            "controller_prefix": request.script_root if getattr(g, 'controller_ui', False) else '',
        }

    def host_available(require_idle=False):
        if maintenance.status(settings)["busy"]:
            raise ValueError("Wait for the pending update or routing task to finish.")
        if require_idle and app.config["TRAFFICGEN_CONTROLLER"].status().get("status") in ("starting", "running", "stopping"):
            raise ValueError("Stop the workload before changing appliance routing or installing an update.")

    def management_interface():
        return maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {}).get("management_interface", "eth0")

    def routed_payload(payload):
        host_available()
        selected = maintenance.status(settings).get("selected")
        if not selected:
            # Without an appliance route a data-NIC simulator would reach the target over management,
            # around the appliance and NetEm: every result would be meaningless.
            host = urlsplit(str(payload.get("target") or settings.default_target)).hostname or ""
            management = management_interface()
            egress = target_egress(host)
            if egress and egress.get("dev") == management and interfaces(management):
                raise ValueError(f"No appliance route is selected, so traffic to {host} would leave through the management "
                                 f"interface ({management}) and bypass the appliance. Select an appliance under Updates & "
                                 "Appliance Routing, or repair the traffic path.")
        if selected:
            try:
                actual = ip_json("route", "get", selected["target"], *(["from", selected["source"]] if selected.get("policy_table") else []))[0]
            except (OSError, ValueError, IndexError) as exc:
                raise ValueError("Cannot verify the selected appliance route. Verify & select it again.") from exc
            if actual.get("dev") != selected["interface"] or actual.get("gateway") != selected["gateway"] or 'linkdown' in actual.get('flags', []):
                raise ValueError("The selected appliance route is missing or changed. Verify & select it again.")
            payload.setdefault("target", f"http://{selected['target']}:8090")
            if urlsplit(str(payload.get("target", ""))).hostname != selected["target"]:
                raise ValueError("Workload target must match the selected appliance's benchmark route.")
        return payload

    def start_workload(payload):
        payload = dict(payload)
        lease_seconds = payload.pop('controller_lease_seconds', None)
        if lease_seconds is not None and (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 15 <= lease_seconds <= 120):
            raise ValueError('Controller lease must be 15–120 seconds.')
        payload = routed_payload(payload)
        selected = maintenance.status(settings).get("selected") or {}
        if selected.get("policy_table"):
            if not traffic_paths.policy_status(selected, management=management_interface())["active"]:
                raise ValueError("Selected path policy is inactive; workload fallback is refused.")
            result = app.config["TRAFFICGEN_CONTROLLER"].start(payload, traffic_path=traffic_paths.metadata(selected))
        else:
            result = app.config["TRAFFICGEN_CONTROLLER"].start(payload)
        if lease_seconds is not None:
            app.config['TRAFFICGEN_CONTROLLER'].bind_controller_lease(result['run']['run_id'], lease_seconds)
            return app.config['TRAFFICGEN_CONTROLLER'].status()
        return result

    def with_target_finding(summary, status):
        finding = target_finding((status.get("run") or {}).get("target_info"), __version__)
        if finding and summary.get("diagnosis"):
            summary["diagnosis"]["findings"].insert(0, finding)
        return summary

    def saved_appliances():
        rows = maintenance.read_json(settings.runtime_dir / "appliances.json", [])
        return rows if isinstance(rows, list) else []

    appliance_inventory_lock = threading.RLock()

    @app.before_request
    def serialize_routing_requests():
        if request.method in ("POST", "PUT", "DELETE") and request.endpoint in (
                "api_network_appliance_save", "api_network_select", "system_action", "api_network_repair",
                "api_workload_start", "workload_start_ui", "api_path_configure", "api_path_select", "api_path_remove", "path_action_ui", "network_setup_action", "network_setup_probe", "network_setup_dhcp"):
            appliance_inventory_lock.acquire()
            g.appliance_inventory_lock = True

    @app.teardown_request
    def release_routing_request(_error):
        if getattr(g, "appliance_inventory_lock", False):
            g.appliance_inventory_lock = False
            appliance_inventory_lock.release()

    def network_readiness(check_target=True, inventory=None):
        """Whether workloads reach the controlled target through the appliance right now."""
        state = maintenance.status(settings)
        management = management_interface()
        rows = inventory if inventory is not None else interfaces(management)
        result = path_readiness(state.get("selected"), management, rows, urlsplit(settings.default_target).hostname or "198.18.0.1",
                                state.get("interface_configs"), saved_appliances())
        result.update(busy=state["busy"], job=state.get("job") or {}, checked_at=time.time())
        if check_target and result["ready"]:
            if state.get("selected", {}) and state["selected"].get("policy_table"):
                observation = traffic_paths.probe(state["selected"], management)
                ok, message = observation["reachable"] is True, observation["message"]
            else:
                ok, message = target_health(result["path"]["target"])
            result["target"] = {"ok": ok, "message": message}
            if not ok:
                result.update(ready=False, message=message + " The route is in place: check the appliance policy and its WAN links.")
        return result

    def enqueue_repair():
        host_available()
        readiness = network_readiness(check_target=False)
        if not readiness["repairable"]:
            raise ValueError(readiness["message"] if not readiness["ready"] else "The traffic path is already in place.")
        selected = maintenance.status(settings).get("selected")
        return maintenance.enqueue(settings, "repair", {} if selected else {"appliance": saved_appliances()[0]})

    app.config["TRAFFICGEN_NETWORK_READINESS"] = network_readiness
    app.config["TRAFFICGEN_REPAIR"] = enqueue_repair

    path_health_cache = {"checked_at": None, "paths": []}
    path_health_lock = threading.Lock()
    path_health_clock = None

    def path_health():
        nonlocal path_health_clock
        # Only one collector; callers can read last known data while it runs.
        if not path_health_lock.acquire(blocking=False):
            return dict(path_health_cache)
        try:
            if path_health_clock is None or time.monotonic() - path_health_clock >= 30:
                paths = maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, [])
                observations = [traffic_paths.probe(path, management_interface()) for path in paths]
                path_health_cache.update(checked_at=time.time(), paths=observations)
                path_health_clock = time.monotonic()
            return dict(path_health_cache)
        finally:
            path_health_lock.release()

    app.config["TRAFFICGEN_PATH_HEALTH"] = path_health

    def system_snapshot():
        state = maintenance.status(settings)
        state["target"] = maintenance.read_json(maintenance.ADMIN_DIR / remote_target.STATUS_FILE,
                                               {"connected": False, "release": {}, "job": {}})
        state["target"].update(remote_target.compare_versions(state["target"].get("version"), __version__))
        state["workload_active"] = app.config["TRAFFICGEN_CONTROLLER"].status().get("status") in ("starting", "running", "stopping")
        try:
            policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
            state["network"] = discover(policy.get("management_interface", "eth0"))
            state["route_health"] = route_status(state.get("selected"), policy.get("management_interface", "eth0"), state["network"]["interfaces"])
            state["readiness"] = network_readiness(check_target=False, inventory=state["network"]["interfaces"])
            inventory = maintenance.read_json(maintenance.ADMIN_DIR / "proxmox-inventory.json", {})
            state["network"]["candidates"] = enrich_candidates(state["network"]["candidates"], state["network"]["interfaces"], inventory)
            scanned = maintenance.read_json(maintenance.ADMIN_DIR / "discovery.json", {})
            cached = {(r.get("interface"), r.get("gateway"), r.get("mac")): r for r in scanned.get("candidates", [])}
            state["network"]["candidates"] = [{**r, **cached.get((r["interface"], r["gateway"], r.get("mac")), {})} for r in state["network"]["candidates"]]
            state["scanned_candidates"] = state["network"]["candidates"]
            state["discovery_warning"] = scanned.get("warning")
            state["scanned_at"] = scanned.get("scanned_at")
        except (OSError, ValueError) as exc:
            state["network"] = {"interfaces": [], "candidates": [], "error": str(exc)}
            state["route_health"] = {"state": "error", "active": False, "message": "Cannot inspect live network state: " + str(exc)}
            state["readiness"] = {"ready": False, "repairable": False, "saved_route": bool(state.get("selected")),
                                  "message": "Cannot inspect live network state: " + str(exc)}
        state["appliances"] = saved_appliances()
        return state

    @app.get("/settings/system")
    @admin_required
    def system_settings():
        return render_template("system.html", page="system", system=system_snapshot(), vendors=VENDORS)

    @app.get("/settings/system/data")
    @admin_required
    def system_data():
        # The update screen polls this across the restart and reloads once the new version answers.
        return jsonify({**system_snapshot(), "version": __version__})

    @app.post("/settings/system/action")
    @admin_required
    def system_action():
        action = request.form.get("action", "")
        try:
            host_available(require_idle=action in ("route", "clear_route", "install_update", "configure_interface", "forget_interface", "target_install"))
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
                rows.append({**route, "name": name, "vendor": vendor, "model": request.form.get("model", "")[:80], "firmware": request.form.get("firmware", "")[:40], "id": secrets.token_hex(8)})
                _write_secret(settings.runtime_dir / "appliances.json", json.dumps(rows))
                flash("Appliance saved. Select it below to verify and apply its traffic route.", "success")
            elif action == "repair":
                enqueue_repair()
                flash("Repair queued: restoring the data interface and the appliance route, then checking the target.", "info")
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
                elif action == "configure_inventory":
                    policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
                    payload = validate_config(dict(request.form), policy.get("management_interface", "eth0"))
                elif action == "configure_interface":
                    policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
                    payload = validate_interface(dict(request.form), policy.get("management_interface", "eth0"))
                elif action == "forget_interface":
                    payload = {"interface": request.form.get("interface", "")}
                elif action == "install_update":
                    if not maintenance.release_status().get("available"):
                        raise ValueError(f"v{__version__} is already the latest checked release. Check for updates again.")
                    payload = {"tag": request.form.get("tag", "")}
                elif action == "configure_target":
                    policy = maintenance.read_json(maintenance.ADMIN_DIR / "policy.json", {})
                    payload = remote_target.validate_config(dict(request.form), policy.get("management_interface", "eth0"))
                elif action == "target_install":
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
            "media_mode": request.form.get("media_mode") or "strict",
        }
        try:
            start_workload(payload)
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
            "media_mode": request.form.get("media_mode"),
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
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        return render_template(
            "dem.html",
            page="dem",
            status=status,
            summary=with_target_finding(dem_summary(settings.database_path, run_id=run_id, window_seconds=60, media_mode=media_mode), status),
            runs=recent_runs(settings.database_path, 12),
            experience_policy=load_policy(settings.database_path),
            experience_applications=profile_payload()["applications"],
        )

    @app.post("/experience/settings")
    @admin_required
    def experience_settings_ui():
        try:
            controller = app.config["TRAFFICGEN_CONTROLLER"]
            with controller.lock:
                if controller.status().get("status") in ("starting", "running", "stopping"):
                    raise ValueError("Stop the workload before changing experience targets.")
                policy = load_policy(settings.database_path)
                raw = {name: {key: (request.form.get(name + "." + key) == "on" if key == "critical"
                                   else request.form.get(name + "." + key))
                              for key in fields} for name, fields in policy.items()}
                save_policy(settings.database_path, raw)
            flash("Experience targets saved. Future measurements use these targets.", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dem_page"))

    @app.get("/ui/status")
    @admin_required
    def ui_status():
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        status['selected_path'] = maintenance.status(settings).get('selected') or {}
        return jsonify(status)

    @app.get("/dem/data")
    @admin_required
    def dem_data():
        try:
            minutes = max(1, min(1440, int(request.args.get("minutes", "15"))))
        except ValueError:
            return {"error": "minutes must be an integer."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        return jsonify(
            {
                "timestamp": time.time(),
                "users": status.get("users", 0),
                "summary": with_target_finding(dem_summary(
                    settings.database_path,
                    run_id=run_id,
                    window_seconds=min(3600, minutes * 60),
                    media_mode=media_mode,
                ), status),
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

    @app.get("/api/v1/network/readiness")
    @bearer_required
    def api_network_readiness():
        return jsonify(network_readiness())

    @app.post("/api/v1/network/repair")
    @bearer_required
    def api_network_repair():
        try:
            readiness = network_readiness(check_target=False)
            if readiness["ready"]:
                return jsonify({"repair": "not_needed", "readiness": readiness}), 200
            return jsonify(enqueue_repair()), 202
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    @app.put("/api/v1/network/appliances/<appliance_id>")
    @bearer_required
    def api_network_appliance_save(appliance_id):
        """Idempotent registration from NetEm; does not change live routing."""
        try:
            if not re.fullmatch(r"[a-f0-9]{16,32}", appliance_id):
                raise ValueError("Invalid appliance inventory ID.")
            data = request.get_json(silent=True)
            allowed = {"name", "vendor", "model", "firmware", "interface", "gateway", "target"}
            if not isinstance(data, dict) or set(data) - allowed:
                raise ValueError("Provide appliance metadata and a constrained target route only.")
            host_available(require_idle=True)
            with appliance_inventory_lock:
                route = validate_route(data, management_interface())
                name = str(data.get("name") or "").strip()
                vendor = data.get("vendor")
                if not name or len(name) > 80 or vendor not in VENDORS:
                    raise ValueError("Provide a supported vendor and name (up to 80 characters).")
                rows = saved_appliances()
                old = next((row for row in rows if row["id"] == appliance_id), None)
                if not old and len(rows) >= 100:
                    raise ValueError("Inventory is limited to 100 appliances.")
                record = dict(route, id=appliance_id, name=name, vendor=vendor,
                              model=str(data.get("model") or "")[:80], firmware=str(data.get("firmware") or "")[:40])
                rows = [row for row in rows if row["id"] != appliance_id] + [record]
                _write_secret(settings.runtime_dir / "appliances.json", json.dumps(rows))
            return jsonify({"appliance": record, "routing_changed": False}), 200
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    @app.get("/api/v1/target/health")
    @bearer_required
    def api_target_health():
        """Fresh target management service status, separately from the workload path."""
        config = maintenance.read_json(maintenance.ADMIN_DIR / remote_target.SECRET_FILE)
        if not config:
            return jsonify({"configured": False, "connected": False, "checked_at": time.time()})
        try:
            trusted = remote_target.validate_config(config, management_interface())
            reply = remote_target.Client(trusted).request("GET", "/api/v1/status")
            public = remote_target.public_status(reply, trusted)
            public.update(configured=True, **remote_target.compare_versions(public.get("version"), __version__))
            return jsonify(public)
        except (ValueError, OSError):
            return jsonify({"configured": True, "connected": False, "checked_at": time.time(),
                            "message": "Cannot verify target management service. Check its connection and certificate."})

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
            # New selection gets a source-bound policy as well as the existing
            # route projection, so probes on other interfaces remain independent.
            policy = {key: selected[key] for key in ("id", "name", "interface", "gateway", "target")}
            if selected.get("source"):
                policy["source"] = selected["source"]
            policy.update(mode="appliance", select=True)
            return jsonify(maintenance.enqueue(settings, "configure_path", policy)), 202
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    @app.get("/api/v1/network/paths")
    @bearer_required
    def api_paths():
        return jsonify({"paths": maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, []),
                        "selected": maintenance.status(settings).get("selected")})

    @app.get("/api/v1/network/paths/health")
    @bearer_required
    def api_path_health():
        return jsonify(path_health())

    @app.put("/api/v1/network/paths/<path_id>")
    @bearer_required
    def api_path_configure(path_id):
        try:
            host_available(require_idle=True)
            raw = request.get_json(silent=True)
            if not isinstance(raw, dict) or "id" in raw or "select" in raw:
                raise ValueError("Provide a traffic path definition; workload selection is a separate action.")
            traffic_paths.validate(dict(raw, id=path_id), management_interface())
            return jsonify(maintenance.enqueue(settings, "configure_path", dict(raw, id=path_id))), 202
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    @app.post("/api/v1/network/paths/select")
    @bearer_required
    def api_path_select():
        try:
            host_available(require_idle=True)
            raw = request.get_json(silent=True)
            if not isinstance(raw, dict) or set(raw) != {"id"}:
                raise ValueError("Choose a configured path ID.")
            if not any(path["id"] == raw["id"] for path in maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, [])):
                raise ValueError("Choose a configured traffic path.")
            return jsonify(maintenance.enqueue(settings, "select_path", {"id": raw["id"]})), 202
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    @app.delete("/api/v1/network/paths/<path_id>")
    @bearer_required
    def api_path_remove(path_id):
        try:
            host_available(require_idle=True)
            if not re.fullmatch(r"[a-f0-9]{16,32}", path_id):
                raise ValueError("Invalid path ID.")
            return jsonify(maintenance.enqueue(settings, "remove_path", {"id": path_id})), 202
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}, 409

    @app.get("/traffic-paths")
    @admin_required
    def traffic_paths_ui():
        paths = maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, [])
        saved = maintenance.read_json(settings.runtime_dir / network_setup.FILE, {})
        drafts = [row for row in saved.values() if not any(path.get('id') == row.get('id') and all(
            path.get(key, '') == row.get(key, '') for key in ('source', 'interface', 'gateway', 'target', 'mode', 'wan_id')) for path in paths)]
        return render_template("traffic_paths.html", page="paths", paths=paths, drafts=drafts,
                               selected=maintenance.status(settings).get("selected") or {}, health=dict(path_health_cache), now=time.time(),
                               job=maintenance.status(settings).get("job") or {}, new_id=secrets.token_hex(16))

    def setup_page(overrides=None):
        saved = maintenance.read_json(settings.runtime_dir / network_setup.FILE, {})
        saved.update(overrides or {})
        state = maintenance.status(settings)
        error = None
        try:
            inventory = interfaces(management_interface())
        except (ValueError, OSError) as exc:
            inventory, error = [], str(exc)
        paths = maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, [])
        hints = maintenance.read_json(settings.runtime_dir / "setup-probe-hints.json", {})
        for role, kind in (("appliance", "appliance"), ("wan1", "isp1"), ("wan2", "isp2")):
            hints.setdefault(kind, saved.get(role, {}).get("gateway", ""))
        if not hints.get("appliance") and saved_appliances():
            hints["appliance"] = saved_appliances()[0].get("gateway", "")
        for kind, wan in (("isp1", "wan1"), ("isp2", "wan2")):
            if not hints.get(kind):
                hints[kind] = next((path["gateway"] for path in paths if path.get("mode") == "direct_netem" and path.get("wan_id") == wan), "")
        cards = network_setup.cards(saved, inventory, paths, management_interface())
        selected_nic = next((nic for nic in inventory if nic['interface'] == request.values.get('interface')), inventory[0] if inventory else None)
        chosen_role = request.values.get('role')
        if chosen_role not in {row[0] for row in network_setup.ROLES}:
            matches = [card for card in cards if card.get('id') and selected_nic and card['interface'] == selected_nic['interface']]
            chosen_role = matches[0]['role'] if len(matches) == 1 else None
        active = next((dict(card) for card in cards if card['role'] == chosen_role), None)
        leases = {nic:data_dhcp.lease(maintenance, nic) for nic in maintenance.read_json(maintenance.ADMIN_DIR / data_dhcp.FILE, {})}
        if active and selected_nic:
            current = leases.get(selected_nic['interface'], {})
            if active['interface'] != selected_nic['interface']:
                active.update(interface=selected_nic['interface'], address='', gateway='', path=None)
            active['nic'] = selected_nic
            if current.get('state') == 'bound':
                active.update(address=current['address'], gateway=current.get('gateway',''))
            elif len(selected_nic['addresses']) == 1:
                active['address'] = selected_nic['addresses'][0]
            if not active['gateway'] and active['mode'] == 'appliance':
                active['gateway'] = next((row['gateway'] for row in saved_appliances() if row['interface'] == selected_nic['interface']), '')
            if not active['gateway'] and active['address']:
                try:
                    active['gateway'] = next((row['gateway'] for row in ip_json('route','show','dev',selected_nic['interface']) if row.get('dst') == 'default' and row.get('gateway')), '')
                except (OSError,ValueError):
                    pass
            if not active['gateway']:
                report = maintenance.read_json(maintenance.ADMIN_DIR / setup_probe.FILE, {})
                if time.time() - report.get('checked_at',0) <= 900:
                    kind = 'appliance' if active['role'] == 'appliance' else ('isp1' if active['role'] in ('wan1','baseline1') else 'isp2')
                    active['gateway'] = next((observation['gateway'] for nic in report.get('interfaces',[]) if nic['interface'] == selected_nic['interface']
                                             for observation in nic.get('observations',[]) if observation.get('reachable') is True and observation.get('kind') == kind), '')
        return render_template("network_setup.html", page="network_setup", system=state,
                               cards=cards, active=active, selected_nic=selected_nic, leases=leases,
                               interfaces=inventory, management=management_interface(), network_error=error,
                               probe_report=maintenance.read_json(maintenance.ADMIN_DIR / setup_probe.FILE, {}), probe_hints=hints, now=time.time(),
                               busy=state["busy"] or app.config["TRAFFICGEN_CONTROLLER"].status().get("status") in ("starting", "running", "stopping"))

    @app.get('/settings/network-setup/status')
    @admin_required
    def network_setup_status():
        state = maintenance.status(settings)
        enabled = maintenance.read_json(maintenance.ADMIN_DIR / data_dhcp.FILE, {})
        busy = state['busy'] or app.config['TRAFFICGEN_CONTROLLER'].status().get('status') in ('starting','running','stopping')
        return jsonify(busy=busy, job=state['job'], leases={nic:data_dhcp.lease(maintenance,nic) for nic in enabled})

    @app.post('/settings/network-setup/dhcp')
    @admin_required
    def network_setup_dhcp():
        interface = request.form.get('interface','')
        try:
            host_available(require_idle=True)
            data_dhcp.validate_nic(SimpleNamespace(management=management_interface,interfaces=interfaces), interface)
            if request.form.get('action','start') not in ('start','stop'):
                raise ValueError('Choose automatic addressing or stop DHCP.')
            action = 'stop_data_dhcp' if request.form.get('action') == 'stop' else 'data_dhcp'
            maintenance.enqueue(settings, action, {'interface':interface})
            flash('Automatic addressing task queued. This page will update when the router responds.', 'info')
        except (ValueError,OSError) as exc:
            flash(str(exc),'error')
        return redirect(url_for('network_setup_ui',interface=interface,role=request.form.get('role','')))

    @app.post("/settings/network-setup/probe")
    @admin_required
    def network_setup_probe():
        try:
            host_available(require_idle=True)
            hints = {kind: request.form.get(kind + "_gateway", "").strip() for kind in setup_probe.HINTS}
            payload = {"gateways": [{"kind": kind, "gateway": gateway} for kind, gateway in hints.items() if gateway]}
            setup_probe.validate(payload, address_table(), management_interface())
            maintenance.enqueue(settings, "probe_setup", payload)
            _write_secret(settings.runtime_dir / "setup-probe-hints.json", json.dumps(hints))
            flash("Interface probes queued. Refresh status when the task completes to see evidence and recommendations.", "info")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("network_setup_ui") + "#interface-probes")

    @app.get("/settings/network-setup")
    @admin_required
    def network_setup_ui():
        return setup_page()

    @app.post("/settings/network-setup")
    @admin_required
    def network_setup_action():
        row = None
        try:
            host_available(require_idle=True)
            if request.form.get("wiring_confirmed") != "1":
                raise ValueError("Confirm this interface is attached to the bridge shown in Proxmox.")
            saved = maintenance.read_json(settings.runtime_dir / network_setup.FILE, {})
            previous = saved.get(request.form.get("role"))
            if not previous:
                existing = network_setup.cards({}, interfaces(management_interface()),
                    maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, []), management_interface())
                previous = next((card for card in existing if card["role"] == request.form.get("role")), None)
            row = network_setup.definition(request.form, previous, management_interface())
            action = request.form.get("action")
            if action not in ("save", "configure", "save_interface"):
                raise ValueError("Choose Save interface or Verify traffic path.")
            if any(other.get("source") == row["source"] for role, other in saved.items() if role != row["role"]):
                raise ValueError("Each path needs its own simulator source address.")
            if action in ("configure", "save_interface"):
                validate_interface({"interface": row["interface"], "address": row["address"]}, management_interface())
                task = {"path": network_setup.payload(row), "address": row["address"]}
                if action == "save_interface":
                    task["verify"] = False
                maintenance.enqueue(settings, "setup_path", task)
            saved[row["role"]] = row
            _write_secret(settings.runtime_dir / network_setup.FILE, json.dumps(saved))
            flash("Setup queued: enabling and saving the interface, then verifying the path. Refresh status for the result." if action == "configure"
                  else "Interface save queued without verification. It will remain unverified until the traffic path check passes." if action == "save_interface"
                  else "Plan saved. Verify traffic path applies it inside the simulator.", "warning" if action == "save_interface" else "success")
            return redirect(url_for("network_setup_ui", interface=row['interface'], role=row['role']))
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
            # Keep entered fields visible after a failed submission.
            role = request.form.get("role")
            allowed = {item[0] for item in network_setup.ROLES}
            override = {role: {key: request.form.get(key, "") for key in ("name", "bridge", "interface", "address", "gateway", "target", "wan_id")}} if role in allowed else {}
            return setup_page(override), 400

    @app.post("/traffic-paths/action")
    @admin_required
    def path_action_ui():
        try:
            host_available(require_idle=True)
            action = request.form.get("action")
            if action == "configure":
                payload = {key: request.form.get(key, "") for key in ("id", "name", "mode", "wan_id", "interface", "source", "gateway", "target")}
                traffic_paths.validate(payload, management_interface())
                maintenance.enqueue(settings, "configure_path", payload)
            elif action in ("select", "remove"):
                path_id = request.form.get("id", "")
                if not any(path["id"] == path_id for path in maintenance.read_json(maintenance.ADMIN_DIR / traffic_paths.FILE, [])):
                    raise ValueError("Choose a configured traffic path.")
                maintenance.enqueue(settings, action + "_path", {"id": path_id})
            else:
                raise ValueError("Unknown path action.")
            flash("Traffic path task queued. Refresh to inspect verification results.", "success")
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("traffic_paths_ui"))

    # ----- Versioned integration API -----

    @app.get("/api/v1/health")
    def api_health():
        return {
            "service": "netem-traffic-simulator",
            "version": __version__,
            "api_version": "v1",
            "status": "ok",
            "instance_name": settings.instance_name,
            # NetEm compares this with its own clock; components must agree on the time.
            "time": time.time(),
            "role": "executor", "controller_ui": True, "controller_leases": True,
            "clock": clock_report(),
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
            status = start_workload(workload_payload())
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
        raw = request.get_json(silent=True) or {}
        if not isinstance(raw, dict) or set(raw) - {'run_id'} or (raw.get('run_id') is not None and not isinstance(raw['run_id'], str)):
            return {'error': 'Invalid workload stop request.'}, 400
        return jsonify(app.config["TRAFFICGEN_CONTROLLER"].stop(expected_run_id=raw.get('run_id')))

    @app.post('/api/v1/controller/heartbeat')
    @bearer_required
    def controller_heartbeat():
        raw = request.get_json(silent=True) or {}
        if (not isinstance(raw, dict) or set(raw) - {'run_id', 'phase'}
                or not isinstance(raw.get('run_id'), str) or not raw['run_id']
                or not isinstance(raw.get('phase', ''), str) or len(raw.get('phase', '')) > 120):
            return {'error': 'Invalid controller heartbeat.'}, 400
        try:
            return jsonify(app.config['TRAFFICGEN_CONTROLLER'].controller_heartbeat(raw['run_id'], raw.get('phase', '')))
        except RuntimeError as exc:
            return {'error': str(exc)}, 409

    @app.get("/api/v1/dem/summary")
    @bearer_required
    def api_dem_summary():
        try:
            window = max(10, min(3600, int(request.args.get("window", "60"))))
        except ValueError:
            return {"error": "window must be an integer from 10 to 3600 seconds."}, 400
        status = app.config["TRAFFICGEN_CONTROLLER"].status()
        run_id = status.get("run", {}).get("run_id") if status.get("run") else None
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        summary = dem_summary(
            settings.database_path,
            run_id=run_id,
            window_seconds=window,
            media_mode=media_mode,
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
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window, media_mode=media_mode)
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
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window, media_mode=media_mode)
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
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window, media_mode=media_mode)
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
        media_mode = (status.get("run") or {}).get("media_mode", "strict")
        summary = dem_summary(settings.database_path, run_id=run_id, window_seconds=window, media_mode=media_mode)
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
                    "interactive_p95_ms": summary["interactive_p95_ms"],
                    "requests_per_second": summary["requests_per_second"],
                    "failures_per_second": summary["failures_per_second"],
                },
                "diagnosis": with_target_finding(summary, status)["diagnosis"],
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
    from .controller_ui import install as install_controller_ui
    install_controller_ui(app)
    return app


app = create_app()
