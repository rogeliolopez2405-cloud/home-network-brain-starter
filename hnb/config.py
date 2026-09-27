"""Runtime settings, read once from the environment.

Nothing here is secret. The vault key is referenced by *path* only; its bytes
are read by hnb.security.vault and never pass through settings or logs.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from hnb import __version__
from hnb.security.netpolicy import DEFAULT_ALLOWED_NETWORKS, parse_networks


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    backup_dir: Path
    log_dir: Path | None
    vault_key_file: Path
    config_file: Path | None = None
    host: str = "0.0.0.0"
    port: int = 8780
    dev: bool = False
    enable_demo: bool = True
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        default_factory=lambda: parse_networks(DEFAULT_ALLOWED_NETWORKS))
    session_idle_s: int = 7 * 24 * 3600
    session_absolute_s: int = 30 * 24 * 3600
    cookie_secure: str = "auto"          # auto | true | false
    poll_interval_s: float = 30.0
    provider_timeout_s: float = 8.0
    backup_keep: int = 14
    version: str = __version__

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hnb.db"

    @property
    def setup_code_file(self) -> Path:
        return self.data_dir / "setup-code"

    @property
    def providers_dir(self) -> Path:
        return self.data_dir / "providers"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "hnb.lock"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        dev = _bool(e.get("HNB_DEV"), False)
        data = Path(e.get("HNB_DATA_DIR") or ("./var/data" if dev else "/var/lib/hnb/data")).resolve()
        backups = Path(e.get("HNB_BACKUP_DIR") or (data.parent / "backups")).resolve()
        logs = e.get("HNB_LOG_DIR")
        key = Path(e.get("HNB_VAULT_KEY_FILE") or ("./var/secrets/vault.key" if dev else "/run/secrets/vault_key"))
        cfg = e.get("HNB_CONFIG_FILE")
        nets = e.get("HNB_ALLOWED_NETWORKS")
        return cls(
            data_dir=data,
            backup_dir=backups,
            log_dir=Path(logs).resolve() if logs else None,
            vault_key_file=key.resolve(),
            config_file=Path(cfg) if cfg else None,
            host=e.get("HNB_HOST", "0.0.0.0"),
            port=int(e.get("HNB_PORT", "8780")),
            dev=dev,
            enable_demo=_bool(e.get("HNB_ENABLE_DEMO"), True),
            allowed_networks=parse_networks(nets.split(",") if nets else DEFAULT_ALLOWED_NETWORKS),
            session_idle_s=int(e.get("HNB_SESSION_IDLE_S", str(7 * 24 * 3600))),
            session_absolute_s=int(e.get("HNB_SESSION_ABSOLUTE_S", str(30 * 24 * 3600))),
            cookie_secure=e.get("HNB_COOKIE_SECURE", "auto").lower(),
            poll_interval_s=float(e.get("HNB_POLL_INTERVAL_S", "30")),
            provider_timeout_s=float(e.get("HNB_PROVIDER_TIMEOUT_S", "8")),
            backup_keep=int(e.get("HNB_BACKUP_KEEP", "14")),
        )
