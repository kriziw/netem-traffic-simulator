from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bind_host: str
    api_port: int
    discovery_port: int
    instance_name: str
    api_key_path: Path
    admin_password_path: Path
    session_secret_path: Path
    runtime_dir: Path
    database_path: Path
    tls_cert: Path
    tls_key: Path
    default_target: str
    target_udp_port: int


def load_settings() -> Settings:
    runtime_dir = Path(
        os.environ.get("TRAFFICGEN_RUNTIME_DIR", "/var/lib/netem-traffic-simulator")
    )
    config_dir = Path(
        os.environ.get("TRAFFICGEN_CONFIG_DIR", "/etc/netem-traffic-simulator")
    )
    return Settings(
        bind_host=os.environ.get("TRAFFICGEN_BIND_HOST", "0.0.0.0"),
        api_port=int(os.environ.get("TRAFFICGEN_API_PORT", "8443")),
        discovery_port=int(os.environ.get("TRAFFICGEN_DISCOVERY_PORT", "47890")),
        instance_name=os.environ.get(
            "TRAFFICGEN_INSTANCE_NAME", "Corporate Traffic Generator"
        ),
        api_key_path=Path(
            os.environ.get("TRAFFICGEN_API_KEY_FILE", str(config_dir / "api.key"))
        ),
        admin_password_path=Path(
            os.environ.get(
                "TRAFFICGEN_ADMIN_PASSWORD_FILE", str(config_dir / "admin.password")
            )
        ),
        session_secret_path=Path(
            os.environ.get(
                "TRAFFICGEN_SESSION_SECRET_FILE", str(config_dir / "session.secret")
            )
        ),
        runtime_dir=runtime_dir,
        database_path=Path(
            os.environ.get(
                "TRAFFICGEN_DATABASE", str(runtime_dir / "traffic-generator.db")
            )
        ),
        tls_cert=Path(
            os.environ.get("TRAFFICGEN_TLS_CERT", str(config_dir / "tls.crt"))
        ),
        tls_key=Path(
            os.environ.get("TRAFFICGEN_TLS_KEY", str(config_dir / "tls.key"))
        ),
        default_target=os.environ.get(
            "TRAFFICGEN_DEFAULT_TARGET", "http://198.18.0.1:8090"
        ).rstrip("/"),
        target_udp_port=int(os.environ.get("TRAFFICGEN_TARGET_UDP_PORT", "9000")),
    )


def _ensure_secret(path: Path, prefix: str = "", bytes_count: int = 32) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    value = prefix + secrets.token_urlsafe(bytes_count)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return value


def ensure_api_key(settings: Settings) -> str:
    return _ensure_secret(settings.api_key_path, "ntg_", 36)


def rotate_api_key(settings: Settings) -> str:
    if settings.api_key_path.exists():
        settings.api_key_path.unlink()
    return ensure_api_key(settings)


def ensure_admin_password(settings: Settings) -> str:
    return _ensure_secret(settings.admin_password_path, "", 18)


def ensure_session_secret(settings: Settings) -> str:
    return _ensure_secret(settings.session_secret_path, "", 48)
