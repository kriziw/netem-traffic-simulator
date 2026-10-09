from __future__ import annotations

import copy
import ipaddress
import logging
import math
import struct
import queue
import random
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

import gevent
from locust import HttpUser, task
from locust.env import Environment

from .database import (
    create_run,
    finish_run,
    record_dem_sample,
    record_transactions,
    recover_runs,
    prune_history,
    update_run,
)
from .dem import dem_summary
from .profiles import (
    ACTIVITY,
    APPLICATIONS,
    PATTERNS,
    PERSONAS,
    WORKLOAD_PROFILES,
    normalized_mix,
)


def weighted_choice(weights: dict) -> str:
    keys = [key for key, value in weights.items() if float(value) > 0]
    if not keys:
        return next(iter(weights))
    values = [float(weights[key]) for key in keys]
    return random.choices(keys, weights=values, k=1)[0]


def bounded_number(value, name, minimum, maximum, integer=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number.") from None
    if isinstance(value, bool) or not math.isfinite(number) or (integer and not number.is_integer()):
        raise ValueError(f"{name} must be a finite {'integer' if integer else 'number'}.")
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return int(number) if integer else number


def combined_application_weights(persona: str, global_mix: dict) -> dict:
    persona_weights = PERSONAS.get(persona, PERSONAS["knowledge_worker"])["applications"]
    combined = {app: float(persona_weights.get(app, 0)) * float(global_mix.get(app, 0))
                for app in APPLICATIONS}
    # A global override can deliberately select an application absent from this persona.
    return combined if sum(combined.values()) > 0 else dict(global_mix)


BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def target_host_is_lab_safe(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    addresses = set()
    for info in infos:
        try:
            addresses.add(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    if not addresses:
        return False
    for address in addresses:
        if (
            any(address in network for network in (
                ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7")
            ))
            or address.is_loopback
            or address.is_link_local
            or address in BENCHMARK_NETWORK
        ):
            continue
        return False
    return True


class CorporateUser(HttpUser):
    abstract = True
    runtime_config = {}

    def on_start(self):
        cfg = self.runtime_config
        self.endpoint_id = "ep-" + uuid.uuid4().hex[:10]
        self.persona = weighted_choice(cfg["personas"])
        self.app_weights = combined_application_weights(
            self.persona, cfg["applications"]
        )
        target = urlsplit(cfg["target"])
        self.udp_host = target.hostname or "127.0.0.1"
        self.udp_port = int(cfg["udp_port"])
        self.client.trust_env = False
        self.client.request = self._bounded_request(self.client.request)
        self.mix_revision = cfg.get("mix_revision", 0)

    @staticmethod
    def _bounded_request(request):
        def bounded(*args, **kwargs):
            kwargs.setdefault("timeout", (5, 10))
            kwargs.setdefault("allow_redirects", False)
            if kwargs.get("catch_response"):
                return request(*args, **kwargs)
            kwargs["catch_response"] = True
            with request(*args, **kwargs) as response:
                if 300 <= response.status_code < 400:
                    response.failure("Controlled target returned an unexpected redirect.")
                return response
        return bounded

    def wait_time(self):
        activity = self.runtime_config.get("activity", "normal")
        bounds = ACTIVITY.get(activity, ACTIVITY["normal"])
        return random.uniform(bounds["wait_min"], bounds["wait_max"])

    def _context(self, application):
        return {
            "run_id": self.runtime_config["run_id"],
            "endpoint_id": self.endpoint_id,
            "persona": self.persona,
            "application": application,
        }

    @task
    def corporate_action(self):
        revision = self.runtime_config.get("mix_revision", 0)
        if revision != self.mix_revision:
            self.persona = weighted_choice(self.runtime_config["personas"])
            self.mix_revision = revision
        self.app_weights = combined_application_weights(self.persona, self.runtime_config["applications"])
        app = weighted_choice(self.app_weights)
        handler = getattr(self, f"_app_{app}", self._app_web_saas)
        handler()

    def _app_web_saas(self):
        size = random.choice([24, 48, 96, 160, 256])
        self.client.get(
            f"/web/page?kb={size}",
            name="web/page",
            context=self._context("web_saas"),
        )
        if random.random() < 0.25:
            self.client.post(
                "/api/action",
                json={"action": "save", "value": random.randint(1, 1000)},
                name="web/api-action",
                context=self._context("web_saas"),
            )

    def _app_collaboration(self):
        self.client.get(
            "/collaboration/poll",
            name="collaboration/poll",
            context=self._context("collaboration"),
        )
        if random.random() < 0.35:
            payload = {"message": "synthetic-presence", "sequence": random.randint(1, 999999)}
            self.client.post(
                "/collaboration/message",
                json=payload,
                name="collaboration/message",
                context=self._context("collaboration"),
            )

    def _app_file_sync(self):
        if random.random() < 0.7:
            size = random.choice([256, 512, 1024, 2048])
            self.client.get(
                f"/files/download?kb={size}",
                name="file-sync/download",
                context=self._context("file_sync"),
            )
        else:
            size = random.choice([128, 256, 512, 1024])
            self.client.post(
                "/files/upload",
                data=b"x" * (size * 1024),
                headers={"Content-Type": "application/octet-stream"},
                name="file-sync/upload",
                context=self._context("file_sync"),
            )

    def _app_developer(self):
        if random.random() < 0.65:
            size = random.choice([512, 1024, 2048, 4096])
            self.client.get(
                f"/developer/artifact?kb={size}",
                name="developer/artifact",
                context=self._context("developer"),
            )
        else:
            self.client.get(
                "/developer/api",
                name="developer/api",
                context=self._context("developer"),
            )

    def _app_updates(self):
        size = random.choice([1024, 2048, 4096, 8192])
        self.client.get(
            f"/updates/package?kb={size}",
            name="updates/package",
            context=self._context("updates"),
        )

    def _app_backup(self):
        size = random.choice([512, 1024, 2048, 4096])
        self.client.post(
            "/backup/upload",
            data=b"b" * (size * 1024),
            headers={"Content-Type": "application/octet-stream"},
            name="backup/upload",
            context=self._context("backup"),
        )

    def _app_dns(self):
        self.client.get(
            "/dns/query?name=corp.example",
            name="dns/query",
            context=self._context("dns"),
        )

    def _udp_burst(self, application, packet_size, packets, interval):
        # Echoed nonce/sequence headers measure received packets and RTT, not
        # the intentional one-second pacing duration or local send success.
        nonce = uuid.uuid4().bytes[:8]
        sent_times = {}
        received = {}
        sock = None
        receiver = None
        receive_errors = []
        error = None
        try:
            family, kind, protocol, _, address = socket.getaddrinfo(self.udp_host, self.udp_port, type=socket.SOCK_DGRAM)[0]
            sock = socket.socket(family, kind, protocol)
            sock.connect(address)
            sock.settimeout(0.1)

            def receive():
                while True:
                    try:
                        data = sock.recv(64)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        receive_errors.append(exc)
                        return
                    if len(data) == 12 and data[:8] == nonce:
                        sequence = struct.unpack("!I", data[8:])[0]
                        if sequence in sent_times:
                            received.setdefault(sequence, (time.perf_counter() - sent_times[sequence]) * 1000)

            receiver = gevent.spawn(receive)
            for sequence in range(packets):
                payload = nonce + struct.pack("!I", sequence) + b"m" * (packet_size - 12)
                sent_times[sequence] = time.perf_counter()
                sock.send(payload)
                gevent.sleep(interval)
            deadline = time.perf_counter() + 0.5
            while len(received) < packets and time.perf_counter() < deadline and not receiver.dead:
                gevent.sleep(0.01)
            if receive_errors:
                error = receive_errors[0]
            elif len(received) != packets:
                error = RuntimeError(f"UDP packet loss: {packets - len(received)}/{packets}")
        except OSError as exc:
            error = exc
        finally:
            if receiver is not None:
                receiver.kill()
            if sock is not None:
                sock.close()
        self.environment.events.request.fire(
            request_type="UDP",
            name=f"{application}/media-burst",
            response_time=sum(received.values()) / len(received) if received else 0,
            response_length=len(received) * 12,
            exception=error,
            context=self._context(application),
        )

    def _app_voice(self):
        # Roughly 64 kbit/s payload rate for one second.
        self._udp_burst("voice", packet_size=160, packets=50, interval=0.02)

    def _app_video(self):
        # Roughly 1.9 Mbit/s payload rate for one second.
        self._udp_burst("video", packet_size=1200, packets=200, interval=0.005)


class WorkloadController:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        recover_runs(settings.database_path)
        self.last_pruned = 0
        self.persistence_error = None
        self.dropped_transactions = 0
        self.environment = None
        self.runner = None
        self.run = None
        self.pattern_greenlet = None
        self.transaction_queue = queue.Queue(maxsize=50000)
        self.stop_event = threading.Event()
        self.writer_thread = threading.Thread(
            target=self._writer_loop, name="trafficgen-db-writer", daemon=True
        )
        self.writer_thread.start()
        self.dem_thread = threading.Thread(
            target=self._dem_loop, name="trafficgen-dem-sampler", daemon=True
        )
        self.dem_thread.start()

    def _writer_loop(self):
        while not self.stop_event.is_set() or not self.transaction_queue.empty():
            try:
                item = self.transaction_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                batch = [item]
                gevent.sleep(0.02)
                while len(batch) < 500:
                    try:
                        batch.append(self.transaction_queue.get_nowait())
                    except queue.Empty:
                        break
                record_transactions(self.settings.database_path, batch)
            except Exception as exc:
                self.persistence_error = str(exc)[:240]
                self.dropped_transactions += len(batch)
                logging.exception("Cannot persist transactions")
            finally:
                for _ in batch:
                    self.transaction_queue.task_done()

    def _dem_loop(self):
        while not self.stop_event.wait(2.0):
            with self.lock:
                run = copy.deepcopy(self.run)
                runner = self.runner
            if not run or run.get("status") not in ("starting", "running"):
                continue
            try:
                summary = dem_summary(
                    self.settings.database_path,
                    run_id=run["run_id"],
                    window_seconds=60,
                )
                users = int(getattr(runner, "user_count", 0) or 0) if runner else 0
                sample = {
                    "timestamp": time.time(),
                    "run_id": run["run_id"],
                    "users": users,
                    "requests_per_second": summary["requests_per_second"],
                    "failures_per_second": summary["failures_per_second"],
                    "availability_pct": summary["availability_pct"],
                    "p50_ms": summary["p50_ms"],
                    "p95_ms": summary["p95_ms"],
                    "experience_score": summary["experience_score"],
                }
                record_dem_sample(self.settings.database_path, sample)
                if time.time() - self.last_pruned >= 3600:
                    prune_history(self.settings.database_path)
                    self.last_pruned = time.time()
            except Exception as exc:
                self.persistence_error = str(exc)[:240]
                logging.exception("Cannot sample DEM")

    def _record_request(
        self,
        request_type,
        name,
        response_time,
        response_length,
        exception,
        context,
        **kwargs,
    ):
        ctx = context or {}
        item = {
            "timestamp": time.time(),
            "run_id": ctx.get("run_id"),
            "endpoint_id": ctx.get("endpoint_id"),
            "persona": ctx.get("persona"),
            "application": ctx.get("application"),
            "request_type": request_type,
            "name": name,
            "success": exception is None,
            "response_time_ms": response_time,
            "response_length": response_length or 0,
            "error": str(exception)[:240] if exception else None,
        }
        try:
            self.transaction_queue.put_nowait(item)
        except queue.Full:
            self.dropped_transactions += 1

    def _validate_start(self, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError("Workload configuration must be an object.")
        if set(raw) - {"profile", "users", "spawn_rate", "activity", "pattern", "target", "personas", "applications"}:
            raise ValueError("Unknown workload configuration fields.")
        profile_id = str(raw.get("profile") or "office")
        profile = WORKLOAD_PROFILES.get(profile_id)
        if not profile:
            raise ValueError("Unknown workload profile.")

        users = bounded_number(raw.get("users", 50), "users", 1, 5000, integer=True)
        spawn_rate = bounded_number(raw.get("spawn_rate", 5), "spawn_rate", 0.1, 1000)
        activity = str(raw.get("activity") or profile["activity"])
        if activity not in ACTIVITY:
            raise ValueError("Unknown activity level.")
        pattern = str(raw.get("pattern") or profile["pattern"])
        if pattern not in PATTERNS:
            raise ValueError("Unknown traffic pattern.")

        target = str(raw.get("target") or self.settings.default_target).rstrip("/")
        parsed = urlsplit(target)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Target port must be 1-65535.") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or port == 0):
            raise ValueError("Target must be an http:// or https:// base URL.")
        if not target_host_is_lab_safe(parsed.hostname):
            raise ValueError(
                "Target override must resolve only to private, link-local, loopback "
                "or RFC2544 198.18.0.0/15 lab addresses."
            )

        personas = normalized_mix(
            raw.get("personas", profile["personas"]), PERSONAS
        )
        applications = normalized_mix(
            raw.get("applications", profile["applications"]), APPLICATIONS
        )

        return {
            "run_id": "run-" + uuid.uuid4().hex[:12],
            "started_at": time.time(),
            "status": "starting",
            "profile": profile_id,
            "target": target,
            "target_users": users,
            "spawn_rate": spawn_rate,
            "activity": activity,
            "pattern": pattern,
            "personas": personas,
            "applications": applications,
            "udp_port": self.settings.target_udp_port,
            "stage": None,
            "mix_revision": 0,
        }

    def start(self, raw: dict):
        with self.lock:
            if self.run and self.run.get("status") in ("starting", "running", "stopping"):
                raise RuntimeError("A workload is already running.")

            run = self._validate_start(raw)
            runtime_config = run
            user_class = type(
                "ConfiguredCorporateUser",
                (CorporateUser,),
                {
                    "abstract": False,
                    "host": run["target"],
                    "runtime_config": runtime_config,
                },
            )
            environment = Environment(user_classes=[user_class])
            environment.events.request.add_listener(self._record_request)
            runner = environment.create_local_runner()

            try:
                create_run(self.settings.database_path, run)
            except Exception:
                runner.quit()
                raise
            self.environment = environment
            self.runner = runner
            self.run = run

            pattern = PATTERNS[run["pattern"]]
            if pattern["stages"]:
                self.pattern_greenlet = gevent.spawn(
                    self._run_pattern,
                    run["run_id"],
                    copy.deepcopy(pattern["stages"]),
                )
            else:
                runner.start(
                    user_count=run["target_users"],
                    spawn_rate=run["spawn_rate"],
                )
                run["status"] = "running"
                update_run(self.settings.database_path, run)
                run["stage"] = {
                    "index": 1,
                    "label": "Steady",
                    "users": run["target_users"],
                    "activity": run["activity"],
                }

            return self.status()

    def _run_pattern(self, run_id: str, stages: list):
        for index, stage in enumerate(stages, start=1):
            wait_seconds = max(0.0, float(stage.get("after", 0)))
            if wait_seconds:
                gevent.sleep(wait_seconds)
            with self.lock:
                if not self.run or self.run["run_id"] != run_id:
                    return
                if self.run.get("status") == "stopping":
                    return
                target_users = min(5000, max(
                    1,
                    round(
                        self.run["target_users"]
                        * float(stage.get("users_factor", 1.0))
                    ),
                ))
                activity = stage.get("activity", self.run["activity"])
                self.run["activity"] = activity
                self.run["status"] = "running"
                self.run["stage"] = {
                    "index": index,
                    "label": f'Stage {index}',
                    "users": target_users,
                    "activity": activity,
                    "users_factor": float(stage.get("users_factor", 1.0)),
                }
                update_run(self.settings.database_path, self.run)
                runner = self.runner
                spawn_rate = self.run["spawn_rate"]
            if runner:
                runner.start(user_count=target_users, spawn_rate=spawn_rate)

    def adjust(self, raw: dict):
        with self.lock:
            if not self.run or self.run.get("status") not in ("starting", "running"):
                raise RuntimeError("No workload is running.")
            if not isinstance(raw, dict):
                raise ValueError("Workload configuration must be an object.")
            if set(raw) - {"users", "spawn_rate", "activity", "personas", "applications"}:
                raise ValueError("Adjust supports users, spawn_rate, activity, personas and applications only.")
            candidate = copy.deepcopy(self.run)
            if "users" in raw:
                candidate["target_users"] = bounded_number(raw["users"], "users", 1, 5000, integer=True)
            if "spawn_rate" in raw:
                candidate["spawn_rate"] = bounded_number(raw["spawn_rate"], "spawn_rate", 0.1, 1000)
            if "activity" in raw:
                if not isinstance(raw["activity"], str) or raw["activity"] not in ACTIVITY:
                    raise ValueError("Unknown activity level.")
                candidate["activity"] = raw["activity"]
            for key, allowed in (("applications", APPLICATIONS), ("personas", PERSONAS)):
                if key in raw:
                    candidate[key] = normalized_mix(raw[key], allowed)
                    candidate["mix_revision"] += 1
            users = min(5000, max(1, round(candidate["target_users"] *
                         (candidate.get("stage") or {}).get("users_factor", 1.0))))
            if candidate.get("stage"):
                candidate["stage"].update(users=users, activity=candidate["activity"])
            update_run(self.settings.database_path, candidate)
            self.run.update(candidate)
            if self.runner:
                self.runner.start(user_count=users, spawn_rate=candidate["spawn_rate"])
            return self.status()

    def stop(self):
        with self.lock:
            if not self.run or self.run.get("status") in ("stopping", "stopped"):
                return self.status()
            run_id = self.run["run_id"]
            self.run["status"] = "stopping"
            runner = self.runner
        if self.pattern_greenlet is not None:
            try:
                self.pattern_greenlet.kill(block=False)
            except Exception:
                pass
            self.pattern_greenlet = None
        if runner:
            runner.quit()
        self.transaction_queue.join()
        finish_run(self.settings.database_path, run_id, "stopped")
        with self.lock:
            self.run["status"] = "stopped"
            self.run["ended_at"] = time.time()
            self.runner = None
            self.environment = None
        return self.status()

    def status(self):
        with self.lock:
            run = copy.deepcopy(self.run)
            runner = self.runner
        users = int(getattr(runner, "user_count", 0) or 0) if runner else 0
        if not run:
            return {
                "persistence_error": self.persistence_error,
                "dropped_transactions": self.dropped_transactions,
                "status": "idle",
                "run": None,
                "users": 0,
                "dem": dem_summary(self.settings.database_path, window_seconds=60),
            }
        dem = dem_summary(
            self.settings.database_path,
            run_id=run["run_id"],
            window_seconds=60,
        )
        dem["endpoint_count"] = len(dem["endpoints"])
        dem["endpoints"] = dict(sorted(dem["endpoints"].items(), key=lambda item: item[1]["experience_score"] if item[1]["experience_score"] is not None else 999)[:200])
        return {
            "persistence_error": self.persistence_error,
            "dropped_transactions": self.dropped_transactions,
            "status": run["status"],
            "run": run,
            "users": users,
            "dem": dem,
        }

    def shutdown(self):
        try:
            self.stop()
        except Exception:
            pass
        self.stop_event.set()
        self.writer_thread.join(timeout=5)
        self.dem_thread.join(timeout=5)
