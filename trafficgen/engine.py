from __future__ import annotations

import copy
import ipaddress
import logging
import math
import os
import struct
import subprocess
import sys
import queue
import random
import socket
import threading
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

import gevent
from locust import HttpUser, task
from locust.env import Environment
# After locust: importing it monkey-patches ssl, which must not be loaded yet.
import requests

from . import __version__
from .database import (
    create_run,
    finish_run,
    record_dem_sample,
    record_transactions,
    recover_runs,
    prune_history,
    update_run,
)
from .database import read_dem_snapshot
from .dem import dem_summary
from .database import save_contract, record_data_loss
from .experience_policy import load_policy, validate_policy, SCORE_MODEL
from .workload import application_weights, compile_manifest
from .rate import RateScheduler
from .operation import Operation
from .database import record_operation_events
from .dem_worker import SNAPSHOT_MAX_AGE_S, fresh_snapshot
from .offload import offloaded
from .diagnosis import MediaFailure, classify_failure, response_details, target_finding
from .profiles import (
    ACTIVITY,
    APPLICATIONS,
    MEDIA_MODES,
    PATTERNS,
    PERSONAS,
    WORKLOAD_PROFILES,
    normalized_mix,
)
from .wire import FULL_ECHO_REQUEST, ADDRESS_REQUEST, ECHO_HEADER_BYTES, unpack_address


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
    return application_weights(persona, global_mix)


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


def validated_media_mode(value) -> str:
    if not isinstance(value, str) or value not in MEDIA_MODES:
        raise ValueError("Unknown media mode; use " + " or ".join(MEDIA_MODES) + ".")
    return value


# The largest bounded object the controlled target serves on /files/download. Paced users pick sizes from the
# list, so their transfers do not run in lockstep.
# Paced bulk requests begun, cumulative. NetEm subtracts the completed ones to count what is still in flight.
PACED_STARTED = {"requests": 0}
# Paced downloads in progress right now, in this process. Decremented however a download ends, so it stays true.
PACED_IN_FLIGHT = {"requests": 0}
# The loop monitor wakes this often: a wake-up later than this is time the event loop was held.
LOOP_INTERVAL_S = 0.2
# How much recent history the health endpoint returns for the calibration log.
TELEMETRY_WINDOW_S = 10
PACED_OBJECT_KB = 4096
PACED_OBJECT_SIZES_KB = (1024, 2048, 3072, 4096)


def claim_sparse_slot(runtime_config):
    """Whether this paced user is one of the run's sparse users, which also send OT-style UDP bursts so loss and jitter
    under the paced load are measured. The first users to start claim exactly the run's sparse slots, so the users that
    download, and the rate they share, are known rather than a random draw."""
    if not runtime_config.get("paced_mbps_per_user"):
        return False
    claimed = runtime_config.get("sparse_claimed", 0)
    if claimed >= runtime_config.get("sparse_slots", 0):
        return False
    runtime_config["sparse_claimed"] = claimed + 1
    return True


class CorporateUser(HttpUser):
    abstract = True
    runtime_config = {}

    def on_start(self):
        cfg = self.runtime_config
        self.endpoint_id = "ep-" + uuid.uuid4().hex[:10]
        self.persona = self._claim_persona(cfg)
        self.rng = random.Random(str(cfg.get("seed", 0)) + ":" + str(cfg.get("endpoint_sequence", 0)))
        self.app_weights = combined_application_weights(
            self.persona, cfg["applications"]
        )
        target = urlsplit(cfg["target"])
        self.udp_host = target.hostname or "127.0.0.1"
        self.udp_port = int(cfg["udp_port"])
        self.client.trust_env = False
        source = (cfg.get("traffic_path") or {}).get("source")
        if source:
            from .traffic_paths import bind_http
            bind_http(self.client, source)
        self.client.request = self._bounded_request(self.client.request)
        self.mix_revision = cfg.get("mix_revision", 0)
        self.sparse_user = claim_sparse_slot(cfg)

    def _claim_persona(self, cfg):
        counts = cfg.get("manifest", {}).get("persona_counts")
        if not counts:
            return weighted_choice(cfg["personas"])
        claimed = cfg.setdefault("persona_claimed", {})
        persona = max(counts, key=lambda key: counts[key] - claimed.get(key, 0))
        claimed[persona] = claimed.get(persona, 0) + 1
        cfg["endpoint_sequence"] = cfg.get("endpoint_sequence", 0) + 1
        return persona

    def on_stop(self):
        claimed = self.runtime_config.get("persona_claimed", {})
        if self.persona in claimed:
            claimed[self.persona] = max(0, claimed[self.persona] - 1)
        if getattr(self, "sparse_user", False):
            self.runtime_config["sparse_claimed"] = max(0, self.runtime_config.get("sparse_claimed", 0) - 1)

    def _bounded_request(self, request):
        def bounded(*args, **kwargs):
            kwargs.setdefault("timeout", (5, 10))
            kwargs.setdefault("allow_redirects", False)
            context = kwargs.setdefault("context", {})
            method = str(args[0] if args else kwargs.get("method", "")).upper()
            url = str(args[1] if len(args) > 1 else kwargs.get("url", ""))
            if "kb=" in url:
                from urllib.parse import parse_qs
                kb = parse_qs(urlsplit(url).query).get("kb", [0])[0]
                context.update(transfer_direction="down", expected_bytes=int(kb) * 1024)
            elif isinstance(kwargs.get("data"), bytes):
                context.update(transfer_direction="up", expected_bytes=len(kwargs["data"]))
            context.setdefault("operation_id", uuid.uuid4().hex)
            context.setdefault("started_at", time.time())
            operation = Operation(self.runtime_config, context, getattr(self.environment, "operation_event", None))
            started = operation.started
            cancelled = False
            try:
                if kwargs.get("catch_response"):
                    return request(*args, **kwargs)
                kwargs["catch_response"] = True
                with request(*args, **kwargs) as response:
                    if 300 <= response.status_code < 400:
                        response.failure("Controlled target returned an unexpected redirect.")
                    return response
            except gevent.GreenletExit:
                cancelled = True
                self.environment.events.request.fire(request_type="HTTP", name=kwargs.get("name", "cancelled"),
                    response_time=(time.monotonic() - started) * 1000, response_length=0,
                    exception=RuntimeError("Operation cancelled when workload stopped"), context=dict(context, cancelled=True))
                raise
            finally:
                operation.close(cancelled)
        return bounded

    def wait_time(self):
        if self.runtime_config.get("paced_mbps_per_user"):
            return 0.2 if getattr(self, "sparse_user", False) else 0
        activity = self.runtime_config.get("activity", "normal")
        bounds = ACTIVITY.get(activity, ACTIVITY["normal"])
        return getattr(self, 'rng', random).uniform(bounds["wait_min"], bounds["wait_max"])

    def _context(self, application):
        return {
            "run_id": self.runtime_config["run_id"],
            "endpoint_id": self.endpoint_id,
            "persona": self.persona,
            "application": application,
            "phase_id": self.runtime_config.get("controller_phase"),
            "path_id": self.runtime_config.get("traffic_path"),
            "contract_epoch": self.runtime_config.get("contract_epoch"),
        }

    def _paced_bulk(self):
        """Calibration traffic: bounded objects back to back, each user paced to its share of the target rate.
        Users start at random offsets and fetch random sizes, so their bursts do not line up. A link or host that
        cannot keep up takes longer, so the achieved rate falls below the target."""
        environment = getattr(self, "environment", None)
        scheduler = getattr(environment, "rate_scheduler", None)
        if scheduler is not None:
            direction, kb, offered_at = scheduler.jobs.get()
            scheduler.dispatched(direction, kb)
            PACED_STARTED["requests"] += 1
            PACED_IN_FLIGHT["requests"] += 1
            try:
                context = dict(self._context("guest_internet"), transfer_direction=direction, expected_bytes=kb * 1024)
                if direction == "down":
                    response = self.client.get(f"/files/download?kb={kb}", name="paced/bulk-download", context=context)
                    delivered = len(response.content) if response.status_code == 200 else 0
                else:
                    response = self.client.post("/files/upload", data=b"u" * (kb * 1024),
                        name="paced/bulk-upload", context=context, headers={"Content-Type": "application/octet-stream"})
                    delivered = kb * 1024 if response.status_code == 200 else 0
                scheduler.completed(direction, delivered)
            finally:
                PACED_IN_FLIGHT["requests"] -= 1
                scheduler.jobs.task_done()
            return
        active = list(getattr(getattr(environment, "runner", None), "user_greenlets", []))
        bulk = sum(1 for greenlet in active if greenlet.args and not getattr(greenlet.args[0], "sparse_user", False))
        rate_bps = self.runtime_config.get("paced_mbps", self.runtime_config["paced_mbps_per_user"] * max(1, bulk)) / max(1, bulk) * 1_000_000
        if not getattr(self, "paced_offset_taken", False):
            self.paced_offset_taken = True
            gevent.sleep(getattr(self, 'rng', random).uniform(0, PACED_OBJECT_KB * 1024 * 8 / rate_bps))
        kb = getattr(self, 'rng', random).choice(PACED_OBJECT_SIZES_KB)
        PACED_STARTED["requests"] += 1
        PACED_IN_FLIGHT["requests"] += 1
        started = time.monotonic()
        try:
            self.client.get(f"/files/download?kb={kb}", name="paced/bulk-download",
                            context=self._context("guest_internet"))
        finally:
            # A download cut short when the run stops still leaves the in-flight count, so the count stays true.
            PACED_IN_FLIGHT["requests"] -= 1
        due = kb * 1024 * 8 / rate_bps
        remaining = due - (time.monotonic() - started)
        if remaining > 0:
            gevent.sleep(remaining)

    @task
    def corporate_action(self):
        if self.runtime_config.get("status") == "stopping":
            gevent.sleep(.05)
            return
        if self.runtime_config.get("paced_mbps_per_user"):
            if getattr(self, "sparse_user", False):
                self._app_ot_telemetry()
            else:
                self._paced_bulk()
            return
        revision = self.runtime_config.get("mix_revision", 0)
        if revision != self.mix_revision:
            claimed = self.runtime_config.get("persona_claimed", {})
            claimed[self.persona] = max(0, claimed.get(self.persona, 0) - 1)
            self.persona = self._claim_persona(self.runtime_config)
            self.mix_revision = revision
        self.app_weights = combined_application_weights(self.persona, self.runtime_config["applications"])
        app = getattr(self, 'rng', random).choices(list(self.app_weights), weights=list(self.app_weights.values()), k=1)[0]
        handler = getattr(self, f"_app_{app}", self._app_web_saas)
        handler()

    def _app_web_saas(self):
        size = getattr(self, 'rng', random).choice([24, 48, 96, 160, 256])
        self.client.get(
            f"/web/page?kb={size}",
            name="web/page",
            context=self._context("web_saas"),
        )
        if getattr(self, 'rng', random).random() < 0.25:
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
        if getattr(self, 'rng', random).random() < 0.35:
            payload = {"message": "synthetic-presence", "sequence": random.randint(1, 999999)}
            self.client.post(
                "/collaboration/message",
                json=payload,
                name="collaboration/message",
                context=self._context("collaboration"),
            )

    def _app_file_sync(self):
        if getattr(self, 'rng', random).random() < 0.7:
            size = getattr(self, 'rng', random).choice([256, 512, 1024, 2048])
            self.client.get(
                f"/files/download?kb={size}",
                name="file-sync/download",
                context=self._context("file_sync"),
            )
        else:
            size = getattr(self, 'rng', random).choice([128, 256, 512, 1024])
            self.client.post(
                "/files/upload",
                data=b"x" * (size * 1024),
                headers={"Content-Type": "application/octet-stream"},
                name="file-sync/upload",
                context=self._context("file_sync"),
            )

    def _app_developer(self):
        if getattr(self, 'rng', random).random() < 0.65:
            size = getattr(self, 'rng', random).choice([512, 1024, 2048, 4096])
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
        size = getattr(self, 'rng', random).choice([1024, 2048, 4096, 8192])
        self.client.get(
            f"/updates/package?kb={size}",
            name="updates/package",
            context=self._context("updates"),
        )

    def _app_backup(self):
        size = getattr(self, 'rng', random).choice([512, 1024, 2048, 4096])
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

    def _udp_burst(self, application, packet_size, packets, interval, full_echo=False):
        # Echoed nonce/sequence headers measure received packets and RTT, not
        # the intentional one-second pacing duration or local send success.
        context = self._context(application)
        context.update(operation_id=uuid.uuid4().hex, started_at=time.time())
        operation = Operation(self.runtime_config, context, getattr(self.environment, "operation_event", None))
        cancelled = False
        nonce = uuid.uuid4().bytes[:8]
        sent_times = {}
        received = {}
        received_bytes = {}
        observed = {}
        packet_paths = {}
        transitions = []
        previous_packet = None
        sock = None
        receiver = None
        receive_errors = []
        error = None
        try:
            family, kind, protocol, _, address = socket.getaddrinfo(self.udp_host, self.udp_port, type=socket.SOCK_DGRAM)[0]
            sock = socket.socket(family, kind, protocol)
            source = (self.runtime_config.get("traffic_path") or {}).get("source")
            if source:
                sock.bind((source, 0))
            sock.connect(address)
            sock.settimeout(0.1)

            def receive():
                nonlocal previous_packet
                while True:
                    try:
                        data = sock.recv(65535)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        receive_errors.append(exc)
                        return
                    if len(data) >= ECHO_HEADER_BYTES and data[:8] == nonce:
                        sequence = struct.unpack("!I", data[8:ECHO_HEADER_BYTES])[0]
                        if sequence in sent_times:
                            received_bytes.setdefault(sequence, len(data))
                            received.setdefault(sequence, (time.perf_counter() - sent_times[sequence]) * 1000)
                        # Newer targets append the source address they saw: the WAN that carried this burst.
                        if full_echo and len(data) > ECHO_HEADER_BYTES and data[ECHO_HEADER_BYTES] in (4, 16):
                            length = data[ECHO_HEADER_BYTES]
                            egress = unpack_address(data[ECHO_HEADER_BYTES + 1:ECHO_HEADER_BYTES + 1 + length])
                        else:
                            egress = unpack_address(data[ECHO_HEADER_BYTES:])
                        observed.setdefault("egress", egress)
                        if egress:
                            stamp = context["started_at"] + time.monotonic() - operation.started
                            entry = packet_paths.setdefault(egress, {"first_observed_at": stamp, "last_observed_at": stamp, "packets": 0})
                            entry.update(last_observed_at=stamp, packets=entry["packets"] + 1)
                            last_paths = getattr(self, "last_packet_paths", {})
                            previous_packet = last_paths.get(application)
                            if previous_packet and previous_packet["egress"] != egress and len(transitions) < 32:
                                transitions.append({"from": previous_packet["egress"], "to": egress,
                                    "earliest_at": previous_packet["at"], "latest_at": stamp,
                                    "basis": "target-observed source on adjacent received packets; reordering can mimic a switch"})
                            last_paths[application] = {"egress": egress, "at": stamp}
                            self.last_packet_paths = last_paths


            receiver = gevent.spawn(receive)
            send_origin = time.perf_counter()
            for sequence in range(packets):
                payload = (nonce + struct.pack("!I", sequence) + (FULL_ECHO_REQUEST if full_echo else ADDRESS_REQUEST)
                           + b"m" * (packet_size - ECHO_HEADER_BYTES - len(ADDRESS_REQUEST)))
                sent_at = time.perf_counter()
                sock.send(payload)
                sent_times[sequence] = sent_at
                gevent.sleep(max(0, send_origin + (sequence + 1) * interval - time.perf_counter()))
            targets = self.runtime_config.get("policy", {}).get(application, {})
            collection_s = max(.5, min(10.0, targets.get("deadline_ms", 500) / 1000 * 2))
            deadline = time.perf_counter() + collection_s
            while len(received) < packets and time.perf_counter() < deadline and not receiver.dead:
                gevent.sleep(0.01)
            if receive_errors:
                error = receive_errors[0]
            elif not received:
                error = MediaFailure("media_no_reply", f"No UDP replies: 0/{len(sent_times)} packets answered")
            # Partial delivery remains transport success. Loss/jitter compliance and
            # utility are scored from packet evidence against the immutable run policy.
        except gevent.GreenletExit:
            cancelled = True
            context["cancelled"] = True
            error = RuntimeError("Media operation cancelled when workload stopped")
        except OSError as exc:
            error = exc
        finally:
            operation.close(cancelled)
            if receiver is not None:
                receiver.kill()
            if sock is not None:
                sock.close()
        # Variation of RTT between adjacent delivered packet sequences. This is
        # round-trip variation, not a one-way jitter measurement or codec MOS.
        variations = [abs(received[sequence] - received[sequence - 1])
                      for sequence in sorted(received) if sequence - 1 in received]
        from .dem import percentile
        from .packet_metrics import histogram
        packet_deadline = self.runtime_config.get("policy", {}).get(application, {}).get("deadline_ms", 500)
        context.update(packet_paths=packet_paths, packet_transitions=transitions,
                       packet_rtt_histogram=histogram(received.values()),
                       packet_variation_histogram=histogram(variations),
                       packets_late=sum(rtt > packet_deadline for rtt in received.values()),
                       packet_deadline_ms=packet_deadline, collection_expired=bool(len(received) < len(sent_times)),
                       packets_sent=len(sent_times), packets_lost=len(sent_times) - len(received),
                       bytes_up=len(sent_times) * packet_size, egress=observed.get("egress"),
                       udp_jitter_ms=percentile(variations, 0.95) if variations else None)
        self.environment.events.request.fire(
            request_type="UDP",
            name=f"{application}/media-burst",
            response_time=sum(received.values()) / len(received) if received else 0,
            response_length=sum(received_bytes.values()),
            exception=error,
            context=context,
        )

        if cancelled:
            raise gevent.GreenletExit

    def _media_action(self, application, packet_size, packets, interval, camera=False):
        if self.runtime_config.get("media_pattern") != "sustained":
            return self._udp_burst(application, packet_size, packets, interval)
        from gevent.pool import Group
        group = Group()
        origin = time.monotonic()
        try:
            for second in range(self.runtime_config.get("media_session_s", 30)):
                if self.runtime_config.get("status") == "stopping":
                    break
                # At most twelve one-second windows, including bounded reply collection.
                # Collection never serializes the next second's sending schedule.
                if len(group) < 12:
                    group.spawn(self._udp_burst, application, packet_size, packets, interval, not camera)
                else:
                    self.runtime_config["media_missed_windows"] = self.runtime_config.get("media_missed_windows", 0) + 1
                gevent.sleep(max(0, origin + second + 1 - time.monotonic()))
            group.join(timeout=11)
        finally:
            group.kill()

    def _app_voice(self):
        # Roughly 64 kbit/s payload rate for one second.
        self._media_action("voice", 160, 50, .02)

    def _app_video(self):
        # Roughly 1.9 Mbit/s payload rate for one second.
        self._media_action("video", 1200, 200, .005)

    # Industry applications reuse the target's bounded endpoints; what makes them
    # distinct is their size, frequency and thresholds in the catalog.

    def _transaction(self, application, name, action):
        self.client.post(
            "/api/action",
            json={"action": action, "value": getattr(self, 'rng', random).randint(1, 1000)},
            name=name,
            context=self._context(application),
        )

    def _download(self, application, name, path, kb_choices):
        self.client.get(f"{path}?kb={getattr(self, 'rng', random).choice(kb_choices)}", name=name, context=self._context(application))

    def _upload(self, application, name, path, kb_choices):
        self.client.post(
            path,
            data=b"u" * (getattr(self, 'rng', random).choice(kb_choices) * 1024),
            headers={"Content-Type": "application/octet-stream"},
            name=name,
            context=self._context(application),
        )

    def _app_ot_telemetry(self):
        # Machine/SCADA polling: 20 short exchanges over one second.
        self._udp_burst("ot_telemetry", packet_size=120, packets=20, interval=0.05)

    def _app_mes(self):
        self._transaction("mes", "mes/transaction", "mes-booking")
        if getattr(self, 'rng', random).random() < 0.3:
            self._transaction("mes", "mes/transaction", "mes-confirmation")

    def _app_erp(self):
        self._download("erp", "erp/screen", "/web/page", [32, 64, 96, 128])
        if getattr(self, 'rng', random).random() < 0.4:
            self._transaction("erp", "erp/posting", "erp-posting")

    def _app_plm_cad(self):
        if getattr(self, 'rng', random).random() < 0.7:
            self._download("plm_cad", "plm/check-out", "/updates/package", [4096, 8192, 12288, 16384])
        else:
            self._upload("plm_cad", "plm/check-in", "/files/upload", [2048, 4096, 8192])

    def _app_pos(self):
        self._transaction("pos", "pos/payment", "payment")

    def _app_wms_scan(self):
        self._transaction("wms_scan", "wms/scan", "scan")

    def _app_emr(self):
        self._download("emr", "emr/chart", "/web/page", [64, 128, 192, 256])
        if getattr(self, 'rng', random).random() < 0.25:
            self._transaction("emr", "emr/update", "chart-update")

    def _app_pacs_imaging(self):
        self._download("pacs_imaging", "pacs/study", "/files/download", [2048, 4096, 6144, 8192])

    def _app_core_banking(self):
        self._transaction("core_banking", "banking/transaction", "transaction")
        if getattr(self, 'rng', random).random() < 0.3:
            self._download("core_banking", "banking/screen", "/web/page", [24, 48])

    def _app_cctv_backhaul(self):
        if self.runtime_config.get("media_pattern") == "sustained":
            return self._media_action("cctv_backhaul", 1200, 200, .005, camera=True)
        self._upload("cctv_backhaul", "cctv/upload", "/backup/upload", [1024, 2048, 3072, 4096])

    def _app_guest_internet(self):
        self._download("guest_internet", "guest/download", "/files/download", [1024, 2048, 3072, 4096])


class WorkloadController:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        recover_runs(settings.database_path)
        self.last_pruned = 0
        self.last_sampled = None
        self.persistence_error = None
        self.dropped_transactions = 0
        self.environment = None
        self.runner = None
        self.run = None
        self.pattern_greenlet = None
        self.target_greenlet = None
        self.rate_scheduler = None
        self.controller_lease = None
        self.transaction_queue = queue.Queue(maxsize=50000)
        self.counters = {"requests": 0, "failures": 0, "bytes": 0, "paced_completed": 0,
                         "paced_wait_ms": 0.0, "paced_transfer_ms": 0.0}
        self.last_worker_restart = 0.0
        from gevent.lock import Semaphore
        self.dem_lock = Semaphore()
        self.last_live_dem = None
        self.timings = {"status_ms": None, "dem_ms": None, "write_ms": None, "write_ms_max": None}
        self.loop_samples = deque(maxlen=round(TELEMETRY_WINDOW_S / LOOP_INTERVAL_S))
        self.target_samples = deque(maxlen=TELEMETRY_WINDOW_S)
        self.stop_event = threading.Event()
        self.writer_thread = threading.Thread(
            target=self._writer_loop, name="trafficgen-db-writer", daemon=True
        )
        self.writer_thread.start()
        self.dem_thread = threading.Thread(
            target=self._dem_loop, name="trafficgen-dem-sampler", daemon=True
        )
        self.dem_thread.start()
        self.monitor_greenlets = [gevent.spawn(self._loop_monitor), gevent.spawn(self._target_pulse_loop)]
        self.dem_worker = self._start_dem_worker()

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
                transactions = [entry for entry in batch if "_flush_event" not in entry and "_operation_event" not in entry]
                started = time.perf_counter()
                events = [entry["_operation_event"] for entry in batch if "_operation_event" in entry]
                if events:
                    offloaded(record_operation_events, self.settings.database_path, events)
                if transactions:
                    offloaded(record_transactions, self.settings.database_path, transactions)
                self._note_timing("write_ms", (time.perf_counter() - started) * 1000.0)
            except Exception as exc:
                self.persistence_error = str(exc)[:240]
                transactions = [entry for entry in batch if "_flush_event" not in entry and "_operation_event" not in entry]
                self.dropped_transactions += len([entry for entry in batch if "_flush_event" not in entry])
                transactions += [entry["_operation_event"] for entry in batch if "_operation_event" in entry]
                for entry in batch:
                    if "_flush_event" in entry:
                        entry["error"] = self.persistence_error
                try:
                    for failed_run in {entry.get("run_id") for entry in transactions}:
                        offloaded(record_data_loss, self.settings.database_path, failed_run,
                                  sum(entry.get("run_id") == failed_run for entry in transactions))
                except Exception:
                    logging.exception("Cannot persist data-loss marker")
                logging.exception("Cannot persist transactions")
            finally:
                for entry in batch:
                    if "_flush_event" in entry:
                        entry["_flush_event"].set()
                    self.transaction_queue.task_done()

    def query_dem(self, database_path=None, **arguments):
        # Explicit test/development opt-out retains deterministic in-process fixtures.
        if os.environ.get("TRAFFICGEN_DEM_WORKER") == "0":
            return offloaded(dem_summary, self.settings.database_path, **arguments)
        from .aggregation import enqueue, read_result
        key = offloaded(enqueue, self.settings.database_path, arguments)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = offloaded(read_result, self.settings.database_path, key)
            if result is not None:
                if "error" in result:
                    raise ValueError(result["error"])
                return result["dem"]
            gevent.sleep(.05)
        from .dem import summarize_rows
        return dict(summarize_rows([]), applications={}, personas={}, endpoints={},
                    diagnosis={"findings": [], "egress": {}}, run_id=arguments.get("run_id"),
                    interactive_p95_ms=None, requests_per_second=0, failures_per_second=0,
                    timestamp=time.time(), evidence_issues=["aggregation_unavailable"], truncated=False)

    def _operation_event(self, event):
        try:
            self.transaction_queue.put_nowait({"_operation_event": event})
        except queue.Full:
            self.dropped_transactions += 1
            gevent.spawn(offloaded, record_data_loss, self.settings.database_path, event.get("run_id"), 1)

    def flush_completed(self):
        """FIFO persistence barrier: future completions cannot postpone a phase query."""
        from gevent.event import Event
        marker = {"_flush_event": Event()}
        self.transaction_queue.put(marker)
        marker["_flush_event"].wait()
        if marker.get("error"):
            raise ValueError("Completed measurements could not be persisted: " + marker["error"])

    def _dem_loop(self):
        while not self.stop_event.wait(2.0):
            with self.lock:
                run = copy.deepcopy(self.run)
                runner = self.runner
            if not run or run.get("status") not in ("starting", "running"):
                continue
            try:
                if self.dem_worker is not None and self.dem_worker.poll() is not None and time.monotonic() - self.last_worker_restart > 10:
                    self.last_worker_restart = time.monotonic()
                    self.dem_worker = self._start_dem_worker()
                snapshot = fresh_snapshot(offloaded(read_dem_snapshot, self.settings.database_path), run["run_id"], contract_epoch=run.get("contract_epoch"))
                if snapshot is None:
                    # The worker has not published this run recently: sample a DEM computed here, off the event loop.
                    summary = self.query_dem(run_id=run["run_id"],
                                        window_seconds=60, diagnosis=False)
                elif snapshot["created"] == self.last_sampled:
                    continue
                else:
                    self.last_sampled = snapshot["created"]
                    summary = snapshot["dem"]
                    self._note_timing("dem_ms", snapshot.get("compute_ms"))
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
                    "experience_score": summary["experience_score"] if summary.get("scoring_complete")
                                        and not self.dropped_transactions and not self.persistence_error else None,
                    "score_model": SCORE_MODEL,
                }
                offloaded(record_dem_sample, self.settings.database_path, sample)
                if time.time() - self.last_pruned >= 3600:
                    offloaded(prune_history, self.settings.database_path)
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
        response=None,
        **kwargs,
    ):
        ctx = context or {}
        details = response_details(response, response_time)
        item = {
            "timestamp": time.time(),
            "run_id": ctx.get("run_id"),
            "endpoint_id": ctx.get("endpoint_id"),
            "persona": ctx.get("persona"),
            "application": ctx.get("application"),
            "transfer_direction": ctx.get("transfer_direction"), "expected_bytes": ctx.get("expected_bytes"),
            "started_at": ctx.get("started_at"), "operation_id": ctx.get("operation_id"), "cancelled": bool(ctx.get("cancelled")),
            "request_type": request_type,
            "name": name,
            "success": exception is None,
            "response_time_ms": response_time,
            "response_length": response_length or 0,
            "error": str(exception)[:240] if exception else None,
            "cause": classify_failure(exception),
            "wait_ms": details["wait_ms"],
            "transfer_ms": details["transfer_ms"],
            "bytes_up": ctx.get("bytes_up", details["bytes_up"]),
            "status_code": details["status_code"],
            "packets_sent": ctx.get("packets_sent"),
            "packets_lost": ctx.get("packets_lost"),
            "udp_jitter_ms": ctx.get("udp_jitter_ms"),
            "egress": ctx.get("egress") or details["egress"],
            "packet_paths": ctx.get("packet_paths"), "packet_transitions": ctx.get("packet_transitions"),
            "packet_rtt_histogram": ctx.get("packet_rtt_histogram"),
            "packet_variation_histogram": ctx.get("packet_variation_histogram"),
            "packets_late": ctx.get("packets_late"), "packet_deadline_ms": ctx.get("packet_deadline_ms"),
            "collection_expired": ctx.get("collection_expired"),
        }
        self.counters["requests"] += 1
        self.counters["failures"] += 0 if exception is None else 1
        self.counters["bytes"] += int(response_length or 0)
        if str(name).startswith("paced/"):
            self.counters["paced_completed"] += 1
            self.counters["paced_wait_ms"] += details["wait_ms"] or 0.0
            self.counters["paced_transfer_ms"] += details["transfer_ms"] or 0.0
        try:
            self.transaction_queue.put_nowait(item)
        except queue.Full:
            self.dropped_transactions += 1
            gevent.spawn(offloaded, record_data_loss, self.settings.database_path, ctx.get("run_id"), 1)

    def _note_timing(self, name, milliseconds):
        self.timings[name] = round(milliseconds, 1)
        if name == "write_ms":
            self.timings["write_ms_max"] = max(self.timings["write_ms_max"] or 0.0, round(milliseconds, 1))

    def telemetry(self):
        """Cumulative counters and the last timings of the work that can hold this process's event loop."""
        return {"requests": self.counters["requests"], "failures": self.counters["failures"],
                "bytes": self.counters["bytes"], "dropped_transactions": self.dropped_transactions,
                "paced_started": PACED_STARTED["requests"], "paced_completed": self.counters["paced_completed"],
                "paced_in_flight": PACED_IN_FLIGHT["requests"],
                "paced_wait_ms": round(self.counters["paced_wait_ms"], 1),
                "paced_transfer_ms": round(self.counters["paced_transfer_ms"], 1),
                # Process CPU seconds, all threads: a share of one core at 100 per cent.
                "cpu_s": round(time.process_time(), 3),
                **self.timings}

    def loop_telemetry(self, seconds=TELEMETRY_WINDOW_S):
        """Recent event-loop lateness and target health latency, as [unix time, value] pairs."""
        cutoff = time.time() - seconds
        return {
            "loop": {"interval_s": LOOP_INTERVAL_S,
                     "samples": [[round(stamp, 3), lag] for stamp, lag in list(self.loop_samples) if stamp >= cutoff]},
            "target_probe": {"samples": [[round(stamp, 3), latency] for stamp, latency in list(self.target_samples)
                                         if stamp >= cutoff]},
        }

    def _loop_monitor(self):
        """How late this process wakes after a short sleep. Every workload user, the HTTP server and the UDP echo run
        on this loop, so a late wake-up is felt by all of them at once."""
        offset = time.time() - time.monotonic()
        while not self.stop_event.is_set():
            current = time.time() - time.monotonic()
            if abs(current - offset) > 1 and self.run:
                self._operation_event({"operation_id": uuid.uuid4().hex, "run_id": self.run["run_id"],
                    "timestamp": time.time(), "kind": "clock_discontinuity", "clock_discontinuity": True})
            offset = current
            started = time.perf_counter()
            gevent.sleep(LOOP_INTERVAL_S)
            lag = max(0.0, (time.perf_counter() - started - LOOP_INTERVAL_S) * 1000.0)
            self.loop_samples.append((time.time(), round(lag, 1)))

    def _target_pulse_loop(self):
        """Once a second during a run, asks the controlled target for its health and records how long it took.
        A target that answers late can stop downloads arriving, so its timing belongs in the calibration log."""
        while not self.stop_event.is_set():
            with self.lock:
                run = dict(self.run) if self.run else None
            if run and run.get("status") in ("starting", "running") and run.get("target"):
                self.target_samples.append((time.time(), self._target_latency(run)))
            gevent.sleep(1.0)

    def _target_latency(self, run):
        """Milliseconds the target took to answer its health check over the run's route, or None if it did not."""
        started = time.perf_counter()
        try:
            source = (run.get("traffic_path") or {}).get("source")
            if source:
                from .traffic_paths import bind_http
                with bind_http(requests.Session(), source) as bound:
                    response = bound.get(run["target"] + "/health", timeout=(2, 2), allow_redirects=False)
            else:
                response = requests.get(run["target"] + "/health", timeout=(2, 2), allow_redirects=False)
            response.raise_for_status()
        except (requests.RequestException, OSError, ValueError, RuntimeError):
            return None
        return round((time.perf_counter() - started) * 1000.0, 1)

    def _start_dem_worker(self):
        """The process that publishes the live DEM. Its standard input stays open for as long as this simulator runs:
        when this process exits, the pipe closes and the worker exits too."""
        if os.environ.get("TRAFFICGEN_DEM_WORKER", "1") == "0":
            return None
        return subprocess.Popen(
            [sys.executable, "-m", "trafficgen.dem_worker", "--database", str(self.settings.database_path)],
            stdin=subprocess.PIPE,
        )

    def _live_dem(self, run):
        """The DEM of the active run: the worker's snapshot while it is fresh, otherwise computed off the event loop."""
        with self.dem_lock:
            snapshot = fresh_snapshot(offloaded(read_dem_snapshot, self.settings.database_path), run["run_id"], contract_epoch=run.get("contract_epoch"))
            if snapshot is not None:
                return snapshot["dem"]
            cached = self.last_live_dem
            if cached and cached[0] == run["run_id"] and 0 <= time.monotonic() - cached[1] < 2:
                return copy.deepcopy(cached[2])
            result = self.query_dem(run_id=run["run_id"], window_seconds=60,
                               media_mode=run.get("media_mode", "strict"))
            self.last_live_dem = (run["run_id"], time.monotonic(), copy.deepcopy(result))
            return result

    def _validate_start(self, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError("Workload configuration must be an object.")
        if set(raw) - {"profile", "users", "spawn_rate", "activity", "pattern", "target", "personas", "applications", "media_mode", "label", "paced_mbps", "sparse_share", "generation_mode", "experience_policy", "seed", "rate_down_mbps", "rate_up_mbps", "completion_grace_s", "media_pattern", "media_session_s"}:
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
        media_pattern = raw.get("media_pattern", "bursts")
        if media_pattern not in ("bursts", "sustained"):
            raise ValueError("media_pattern must be bursts or sustained.")
        media_mode = validated_media_mode(raw.get("media_mode", "strict"))
        label = raw.get("label")
        if label is not None and (not isinstance(label, str) or len(label.strip()) > 120):
            raise ValueError("label must be text of at most 120 characters.")

        directional = any(key in raw for key in ("rate_down_mbps", "rate_up_mbps"))
        if directional and "paced_mbps" in raw:
            raise ValueError("Use directional rates or paced_mbps, not both.")
        generation_mode = raw.get("generation_mode", "rate" if directional or raw.get("paced_mbps") else "users")
        if generation_mode not in ("users", "rate"):
            raise ValueError("generation_mode must be users or rate.")
        rate_down = bounded_number(raw.get("rate_down_mbps", 0), "rate_down_mbps", 0, 1000)
        rate_up = bounded_number(raw.get("rate_up_mbps", 0), "rate_up_mbps", 0, 1000)
        paced_mbps = raw.get("paced_mbps")
        if paced_mbps is not None:
            rate_down = bounded_number(paced_mbps, "paced_mbps", 0.1, 1000)
        total_rate = rate_down + rate_up
        if generation_mode == "rate" and total_rate <= 0:
            raise ValueError("Rate mode requires a positive directional rate or paced_mbps.")
        if generation_mode == "users" and total_rate:
            raise ValueError("Payload rates require rate mode.")
        paced_mbps = total_rate or None
        sparse_share = raw.get("sparse_share")
        if sparse_share is not None and paced_mbps is None:
            raise ValueError("sparse_share needs paced_mbps.")
        sparse_share = 0.0 if sparse_share is None else bounded_number(sparse_share, "sparse_share", 0, 0.5)
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
            "media_mode": media_mode,
            "media_pattern": media_pattern,
            "media_session_s": bounded_number(raw.get("media_session_s", 30), "media_session_s", 1, 300, integer=True),
            # Describes what the run represents, e.g. the site NetEm modelled it on.
            "label": (label or "").strip() or None,
            "completion_grace_s": bounded_number(raw.get("completion_grace_s", 2), "completion_grace_s", 0, 10),
            "generation_mode": generation_mode,
            "seed": bounded_number(raw.get("seed", random.randrange(2**31)), "seed", 0, 2**31 - 1, integer=True),
            "policy": validate_policy(raw["experience_policy"]) if "experience_policy" in raw else load_policy(self.settings.database_path),
            "score_model": SCORE_MODEL,
            "paced_mbps": paced_mbps,
            "rate_down_mbps": rate_down, "rate_up_mbps": rate_up,
            # Exactly this many users send UDP bursts instead of downloads; only the others share the rate.
            "sparse_slots": round(users * sparse_share),
            "sparse_claimed": 0,
            "paced_mbps_per_user": round(paced_mbps / (users - round(users * sparse_share)), 6) if paced_mbps else None,
            "sparse_share": sparse_share,
            "udp_port": self.settings.target_udp_port,
            "stage": None,
            "mix_revision": 0,
        }

    def start(self, raw: dict, traffic_path=None):
        with self.lock:
            if self.run and self.run.get("status") in ("starting", "running", "stopping"):
                raise RuntimeError("A workload is already running.")

            run = self._validate_start(raw)
            if traffic_path:
                run["traffic_path"] = copy.deepcopy(traffic_path)
            if run.get("media_pattern") == "sustained":
                info = self._probe_target(run["run_id"], run["target"], candidate=run)
                if info.get("error") or "media-full-echo-v1" not in info.get("capabilities", []):
                    raise ValueError("Sustained media requires a reachable controlled target advertising media-full-echo-v1; update the target first.")
                run["target_info"] = info
            run["configured_policy"] = copy.deepcopy(run["policy"])
            run["manifest"] = compile_manifest(run)
            if run["media_mode"] == "strict":
                for name in ("voice", "video", "ot_telemetry"):
                    run["policy"][name]["loss_max_pct"] = 0
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
            environment.operation_event = self._operation_event
            environment.events.request.add_listener(self._record_request)
            environment.events.spawning_complete.add_listener(self._reconcile_population)
            runner = environment.create_local_runner()

            try:
                create_run(self.settings.database_path, run)
                save_contract(self.settings.database_path, run, run["started_at"])
            except Exception:
                runner.quit()
                raise
            self.environment = environment
            self.runner = runner
            self.run = run
            self.rate_scheduler = RateScheduler(run).start() if run.get("generation_mode") == "rate" else None
            environment.rate_scheduler = self.rate_scheduler
            self.dropped_transactions = 0
            self.persistence_error = None

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

            self.target_greenlet = gevent.spawn(self._monitor_target, run["run_id"], run["target"])
            return self.status()

    def _reconcile_population(self, user_count=None, **_kwargs):
        if not self.runner or not self.run:
            return
        users = [greenlet.args[0] for greenlet in self.runner.user_greenlets
                 if greenlet.args and hasattr(greenlet.args[0], "persona")]
        desired = dict(self.run["manifest"]["persona_counts"])
        displaced = []
        for user in users:
            if desired.get(user.persona, 0) > 0:
                desired[user.persona] -= 1
            else:
                displaced.append(user)
        for user in displaced:
            available = [persona for persona, count in desired.items() if count > 0]
            if available:
                user.persona = available[0]
                desired[user.persona] -= 1
        self.run["persona_claimed"] = {}
        for index, user in enumerate(users):
            self.run["persona_claimed"][user.persona] = self.run["persona_claimed"].get(user.persona, 0) + 1
            user.mix_revision = self.run.get("mix_revision", 0)
            user.sparse_user = bool(self.run.get("paced_mbps") and index < self.run.get("sparse_slots", 0))
        self.run["sparse_claimed"] = sum(user.sparse_user for user in users)

    def _monitor_target(self, run_id: str, target: str):
        while True:
            with self.lock:
                if (not self.run or self.run["run_id"] != run_id
                        or self.run.get("status") not in ("starting", "running")):
                    return
            self._probe_target(run_id, target)
            gevent.sleep(30)

    def _probe_target(self, run_id: str, target: str, candidate=None):
        # An outdated target cannot report the WAN and may not echo media at all.
        info = {"checked_at": time.time(), "version": None, "capabilities": [], "error": None}
        try:
            source = (((candidate if candidate is not None else self.run) or {}).get("traffic_path") or {}).get("source")
            if source:
                from .traffic_paths import bind_http
                with bind_http(requests.Session(), source) as bound:
                    response = bound.get(target + "/health", timeout=(3, 3), allow_redirects=False)
            else:
                response = requests.get(target + "/health", timeout=(3, 3), allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Target health returned HTTP {response.status_code}")
            payload = response.json()
            if (not isinstance(payload, dict) or payload.get("status") != "ok"
                    or payload.get("service") != "netem-traffic-target"):
                raise ValueError("Response is not a valid controlled-target health report")
            info["version"] = str(payload["version"])[:40] if payload.get("version") else None
            capabilities = payload.get("capabilities")
            info["capabilities"] = [str(item)[:40] for item in capabilities][:20] if isinstance(capabilities, list) else []
        except (requests.RequestException, ValueError) as exc:
            info["error"] = str(exc)[:200]
        with self.lock:
            if self.run and self.run["run_id"] == run_id:
                self.run["target_info"] = info
        return info

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
                if self.run.get("paced_mbps"):
                    self.run["sparse_slots"] = min(target_users - 1, round(target_users * self.run["sparse_share"]))
                    self.run["paced_mbps_per_user"] = self.run["paced_mbps"] / (target_users - self.run["sparse_slots"])
                self.run["manifest"] = compile_manifest(self.run, target_users)
                save_contract(self.settings.database_path, self.run)
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
            if set(raw) - {"users", "spawn_rate", "activity", "personas", "applications", "media_mode"}:
                raise ValueError("Adjust supports users, spawn_rate, activity, personas, applications and media_mode only.")
            candidate = copy.deepcopy(self.run)
            if "users" in raw:
                candidate["target_users"] = bounded_number(raw["users"], "users", 1, 5000, integer=True)
            if "spawn_rate" in raw:
                candidate["spawn_rate"] = bounded_number(raw["spawn_rate"], "spawn_rate", 0.1, 1000)
            if "activity" in raw:
                if not isinstance(raw["activity"], str) or raw["activity"] not in ACTIVITY:
                    raise ValueError("Unknown activity level.")
                candidate["activity"] = raw["activity"]
            if "media_mode" in raw:
                candidate["media_mode"] = validated_media_mode(raw["media_mode"])
                candidate["policy"] = copy.deepcopy(candidate.get("configured_policy", candidate["policy"]))
                if candidate["media_mode"] == "strict":
                    for name in ("voice", "video", "ot_telemetry"):
                        candidate["policy"][name]["loss_max_pct"] = 0
            for key, allowed in (("applications", APPLICATIONS), ("personas", PERSONAS)):
                if key in raw:
                    candidate[key] = normalized_mix(raw[key], allowed)
                    candidate["mix_revision"] += 1
            users = min(5000, max(1, round(candidate["target_users"] *
                         (candidate.get("stage") or {}).get("users_factor", 1.0))))
            if candidate.get("stage"):
                candidate["stage"].update(users=users, activity=candidate["activity"])
            if candidate.get("paced_mbps"):
                candidate["sparse_slots"] = min(users - 1, round(users * candidate["sparse_share"]))
                candidate["paced_mbps_per_user"] = candidate["paced_mbps"] / (users - candidate["sparse_slots"])
            candidate["manifest"] = compile_manifest(candidate, users)
            if any(key in raw for key in ("users", "applications", "personas", "media_mode")):
                save_contract(self.settings.database_path, candidate)
            update_run(self.settings.database_path, candidate)
            candidate["in_flight"] = self.run.get("in_flight", {})
            self.run.update(candidate)
            self.last_live_dem = None
            self._reconcile_population()
            if self.runner:
                self.runner.start(user_count=users, spawn_rate=candidate["spawn_rate"])
            return self.status()

    def bind_controller_lease(self, run_id, seconds):
        with self.lock:
            if not self.run or self.run['run_id'] != run_id:
                raise RuntimeError('The requested execution run changed.')
            self.controller_lease = {'run_id': run_id, 'seconds': seconds, 'deadline': time.monotonic() + seconds}
            self.run['controller_lease_seconds'] = seconds
        gevent.spawn(self._watch_controller_lease, run_id)

    def controller_heartbeat(self, run_id, phase=''):
        with self.lock:
            if not self.run or self.run['run_id'] != run_id or self.run.get('status') not in ('starting', 'running'):
                raise RuntimeError('The executing workload no longer matches the controller test.')
            if self.controller_lease and self.controller_lease['run_id'] == run_id:
                self.controller_lease['deadline'] = time.monotonic() + self.controller_lease['seconds']
            self.run['controller_phase'] = phase
        return self.status()

    def _watch_controller_lease(self, run_id):
        while not self.stop_event.is_set():
            with self.lock:
                lease = self.controller_lease
                if (not lease or lease['run_id'] != run_id or not self.run or self.run['run_id'] != run_id
                        or self.run.get('status') not in ('starting', 'running')):
                    return
                expired = time.monotonic() >= lease['deadline']
            if expired:
                self.stop(expected_run_id=run_id, reason='NetEm controller heartbeat expired')
                return
            gevent.sleep(.5)

    def stop(self, expected_run_id=None, reason=None):
        with self.lock:
            if expected_run_id and (not self.run or self.run['run_id'] != expected_run_id):
                return self.status()
            if not self.run or self.run.get("status") in ("stopping", "stopped"):
                return self.status()
            run_id = self.run["run_id"]
            self.run["status"] = "stopping"
            if reason:
                self.run['stop_reason'] = reason
            runner = self.runner
        if self.target_greenlet is not None:
            self.target_greenlet.kill(block=False)
            self.target_greenlet = None
        if self.pattern_greenlet is not None:
            try:
                self.pattern_greenlet.kill(block=False)
            except Exception:
                pass
            self.pattern_greenlet = None
        if self.rate_scheduler:
            self.rate_scheduler.stop()
        grace_end = time.monotonic() + self.run.get("completion_grace_s", 2.0)
        while self.run.get("in_flight") and time.monotonic() < grace_end:
            gevent.sleep(.02)
        if runner:
            runner.quit()
        gevent.with_timeout(5, self.flush_completed)
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
                "dem": self.query_dem(window_seconds=60),
            }
        started = time.perf_counter()
        dem = self._live_dem(run)
        if self.rate_scheduler:
            run["rate_demand"] = self.rate_scheduler.snapshot()
        pending = run.pop("in_flight", {})
        dem["in_flight"] = {"requests": len(pending), "oldest_age_s": max((time.monotonic() - item.get("started_monotonic", time.monotonic()) for item in pending.values()), default=0)}
        overdue = [item for item in pending.values() if (time.monotonic() - item.get("started_monotonic", time.monotonic())) * 1000 > run.get("policy", {}).get(item.get("application"), {}).get("deadline_ms", 400)]
        if overdue:
            dem["at_risk"] = True
            dem["at_risk_reason"] = "Operations have exceeded their target and are still pending"
            dem["scoring_complete"] = False
            dem.setdefault("evidence_issues", []).append("outstanding_overdue_operations")
        if run.get("media_missed_windows"):
            dem["scoring_complete"] = False
            dem.setdefault("evidence_issues", []).append("media_generator_missed_windows")
        if self.dropped_transactions or self.persistence_error:
            dem["scoring_complete"] = False
            dem.setdefault("evidence_issues", []).append("persistence_unhealthy")
        if dem.get("scoring_complete") is not True:
            dem["score_status"] = "provisional"
            if dem.get("business_acceptance") != "fail":
                dem["business_acceptance"] = "unknown"
        self._note_timing("status_ms", (time.perf_counter() - started) * 1000.0)
        outdated = target_finding(run.get("target_info"), __version__)
        if outdated:
            dem["diagnosis"]["findings"].insert(0, outdated)
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
        for greenlet in self.monitor_greenlets:
            greenlet.kill()
        if self.dem_worker is not None and self.dem_worker.poll() is None:
            self.dem_worker.terminate()
        self.writer_thread.join(timeout=5)
        self.dem_thread.join(timeout=5)
