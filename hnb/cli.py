"""Command line: what the container runs, and what bin/* scripts call.

    python -m hnb.cli serve            run the hub (container entrypoint)
    python -m hnb.cli dev              run locally with ./var and a dev key
    python -m hnb.cli migrate          create/upgrade the database, check integrity
    python -m hnb.cli check            full integrity check
    python -m hnb.cli healthcheck      exit 0 if /api/health answers ok (Docker)
    python -m hnb.cli backup           create + verify a backup
    python -m hnb.cli verify-backup F  verify an archive
    python -m hnb.cli list-backups
    python -m hnb.cli restore F        restore (hub must be stopped)
    python -m hnb.cli setup-code       print the one-time setup code, if setup is open
    python -m hnb.cli provider-config demo '{"simulate":"outage"}'   (testing / acceptance)
    python -m hnb.cli new-provider my_plugs --name "My plugs"      scaffold plugins/my_plugs from the example
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import urllib.request
from pathlib import Path

from hnb import __version__
from hnb.config import Settings
from hnb.security.redact import REDACTOR, RedactingFormatter


def setup_logging(settings: Settings) -> None:
    fmt = RedactingFormatter()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    out = logging.StreamHandler(sys.stderr)
    out.setFormatter(fmt)
    root.addHandler(out)
    if settings.log_dir:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(settings.log_dir / "hnb.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    if not REDACTOR.healthy:
        root.error("Redaction self-test failed; log lines will be withheld.")


def _hold_lock(settings: Settings):
    """Exclusive lock for the running hub; restore refuses while it is held."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    f = settings.lock_file.open("a")
    try:
        import fcntl
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("Another hub process is already using this data directory.")
    except ImportError:
        pass
    return f


def cmd_serve(settings: Settings) -> int:
    import uvicorn

    from hnb.api.app import create_app

    setup_logging(settings)
    lock = _hold_lock(settings)
    log = logging.getLogger("hnb")
    log.info("Home Network BRAIN Starter %s starting on port %s", __version__, settings.port)
    app = create_app(settings)
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False, server_header=False,
                    date_header=False, log_config=None, access_log=False, timeout_graceful_shutdown=10)
    finally:
        lock.close()
    return 0


def cmd_migrate(settings: Settings) -> int:
    from hnb.store import db
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(settings.db_path, snapshot_dir=settings.backup_dir)
    try:
        print(json.dumps({"schema": db.schema_version(conn), "checks": db.check_integrity(conn)}))
    finally:
        conn.close()
    return 0


def cmd_check(settings: Settings) -> int:
    from hnb.store import db
    conn = db.connect(settings.db_path)
    try:
        print(json.dumps(db.check_integrity(conn, full=True)))
    finally:
        conn.close()
    return 0


def cmd_healthcheck(settings: Settings) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{settings.port}/api/health", timeout=4) as r:
            body = json.loads(r.read())
        return 0 if body.get("status") in ("ok", "degraded") else 1
    except Exception:
        return 1


def _looks_like_dev_checkout() -> bool:
    """True when nothing says where the hub's data is, there is no installed hub,
    and a dev hub has data in ./var — the `dev` then `setup-code` workflow."""
    if os.environ.get("HNB_DATA_DIR") or os.environ.get("HNB_DEV"):
        return False
    return not Path("/var/lib/hnb/data").exists() and Path("var/data").is_dir()


def cmd_setup_code(settings: Settings) -> int:
    f = settings.setup_code_file
    if not f.exists():
        if settings.db_path.exists():
            print("Setup is closed: this hub already has an owner.", file=sys.stderr)
        else:
            print(f"No hub data in {settings.data_dir}. Start the hub first, or give this command the same "
                  "HNB_DATA_DIR (and other HNB_* settings) the hub runs with.", file=sys.stderr)
        return 1
    print(f.read_text(encoding="utf-8").strip())
    return 0


def cmd_provider_config(settings: Settings, provider: str, config: str) -> int:
    from hnb.store import db
    from hnb.store.repo import Repo
    conn = db.connect(settings.db_path)
    try:
        with db.tx(conn):
            Repo(conn).set_provider_config(provider, json.loads(config))
    finally:
        conn.close()
    print(f"{provider}: configuration saved; the hub applies it on its next poll.")
    return 0


PROVIDER_ID_RE = __import__("re").compile(r"^[a-z][a-z0-9_]{1,31}$")


class ScaffoldError(ValueError):
    pass


def scaffold_provider(provider_id: str, name: str | None, plugins_dir: Path) -> tuple[Path, str]:
    """Copy examples/providers/acme_plug to <plugins_dir>/<id>/, renamed.

    The test file becomes test_<id>.py: plugin folders aren't packages, so two
    test files with the same name would stop pytest from collecting the suite."""
    import shutil
    if not PROVIDER_ID_RE.match(provider_id):
        raise ScaffoldError("Use 2–32 lowercase letters, digits or _ (starting with a letter), e.g. my_plugs.")
    from hnb.providers.registry import BUILTIN
    if provider_id in BUILTIN:
        raise ScaffoldError(f"{provider_id!r} is a built-in provider id.")
    src = Path(__file__).resolve().parents[1] / "examples" / "providers" / "acme_plug"
    dest = plugins_dir / provider_id
    if dest.exists():
        raise ScaffoldError(f"{dest} already exists.")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "README.md"))
    (dest / "test_conformance.py").rename(dest / f"test_{provider_id}.py")
    cls = "".join(part.capitalize() for part in provider_id.split("_")) + "Adapter"
    display = name or provider_id.replace("_", " ").title()
    import zlib
    port = str(7800 + zlib.crc32(provider_id.encode()) % 900)       # its own simulator port (not the example's 7788)
    for f in ("fake_device.py", f"test_{provider_id}.py", "adapter.py"):
        path = dest / f
        path.write_text(path.read_text(encoding="utf-8").replace("test_conformance.py", f"test_{provider_id}.py")
                        .replace('connect "Acme plugs"', f'connect "{display}"').replace("7788", port),
                        encoding="utf-8", newline="\n")
    a = dest / "adapter.py"
    text = a.read_text(encoding="utf-8")
    text = text.replace('provider_id = "acme_plug"', f'provider_id = "{provider_id}"')
    text = text.replace('display_name = "Acme plugs"', f'display_name = "{display}"')
    text = text.replace('description = "Acme smart plugs through the Acme hub on your network."',
                        f'description = "{display} (scaffolded from the Acme example; say what this module talks to)."')
    text = text.replace("class AcmeAdapter(", f"class {cls}(").replace("ADAPTER = AcmeAdapter", f"ADAPTER = {cls}")
    a.write_text(text, encoding="utf-8", newline="\n")
    readme = [f"# {display}", "",
              "Provider module scaffolded from the Acme example. It works and passes the conformance kit as-is;",
              "now replace the Acme protocol with your service's (ADDING_A_PROVIDER.md, steps 2–14):", "",
              "- `adapter.py`: identity, settings, transport, mapping", f"- `fake_device.py`: a simulator of your service",
              f"- `test_{provider_id}.py`: the conformance harness and your own tests", ""]
    (dest / "README.md").write_text("\n".join(readme), encoding="utf-8", newline="\n")
    return dest, cls


def cmd_new_provider(provider_id: str, name: str | None) -> int:
    """`hnb new-provider <id>`: a working, tested starting point in plugins/<id>/."""
    root = Path(__file__).resolve().parents[1]
    try:
        dest, cls = scaffold_provider(provider_id, name, root / "plugins")
    except ScaffoldError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"Created {dest.relative_to(root)} ({cls}).")
    print(f"  Test it:   python -m pytest plugins/{provider_id}      (12 passed)")
    print(f"  Try it:    python plugins/{provider_id}/fake_device.py  (then connect it in Settings)")
    print("  Then:      replace the Acme protocol in adapter.py and fake_device.py with your service's.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hnb", description="Home Network BRAIN Starter")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "dev", "migrate", "check", "healthcheck", "backup", "list-backups", "setup-code"):
        sub.add_parser(name)
    sub.add_parser("verify-backup").add_argument("archive")
    sub.add_parser("restore").add_argument("archive")
    npv = sub.add_parser("new-provider", help="scaffold a provider module in plugins/")
    npv.add_argument("provider_id")
    npv.add_argument("--name")
    pc = sub.add_parser("provider-config")
    pc.add_argument("provider")
    pc.add_argument("config", help="JSON object")
    args = ap.parse_args(argv)

    if args.cmd == "dev":
        os.environ.setdefault("HNB_DEV", "1")
        os.environ.setdefault("HNB_HOST", "127.0.0.1")
        os.environ.setdefault("HNB_POLL_INTERVAL_S", "15")
        settings = Settings.from_env()
        print(f"Dev hub on http://127.0.0.1:{settings.port}/  data: {settings.data_dir}")
        print(f"Setup code (dev only): python -m hnb.cli setup-code")
        return cmd_serve(settings)

    if args.cmd == "new-provider":
        return cmd_new_provider(args.provider_id, args.name)
    if args.cmd == "setup-code" and _looks_like_dev_checkout():
        os.environ["HNB_DEV"] = "1"                   # `dev` keeps its data in ./var; find it there
    settings = Settings.from_env()
    if args.cmd == "serve":
        return cmd_serve(settings)
    if args.cmd == "migrate":
        return cmd_migrate(settings)
    if args.cmd == "check":
        return cmd_check(settings)
    if args.cmd == "healthcheck":
        return cmd_healthcheck(settings)
    if args.cmd == "setup-code":
        return cmd_setup_code(settings)
    if args.cmd == "provider-config":
        return cmd_provider_config(settings, args.provider, args.config)

    from hnb.ops import backup as ops
    try:
        if args.cmd == "backup":
            r = ops.create_backup(settings, actor="system", source="cli")
            print(json.dumps({k: r[k] for k in ("file", "sha256", "bytes", "verified", "pruned")}, indent=2))
        elif args.cmd == "verify-backup":
            print(json.dumps(ops.verify_backup(Path(args.archive)), indent=2))
        elif args.cmd == "list-backups":
            for p in ops.list_backups(settings):
                print(p.name)
        elif args.cmd == "restore":
            print(json.dumps(ops.restore_backup(settings, Path(args.archive)), indent=2))
    except ops.BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
