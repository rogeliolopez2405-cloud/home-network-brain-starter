"""Where provider adapters come from. Adding a provider never edits the core.

Three sources, in order:

1. Built-ins shipped with the hub (demo, matter).
2. Python packages that declare an entry point in the `hnb.providers` group:
       [project.entry-points."hnb.providers"]
       acme = "hnb_acme.adapter:AcmeAdapter"
3. Plugin folders: every `plugins/<name>/adapter.py` that defines `ADAPTER`
   (the adapter class). `plugins/` ships in the repo and in the image;
   extra folders can be listed in HNB_PLUGIN_DIRS (os.pathsep-separated).

A module that fails to import or breaks the contract is skipped and reported
(Settings → Services, /api/system) — one bad plugin never stops the hub.
Plugins run inside the hub with its privileges: install only code you trust.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from hnb.providers.base import ProviderAdapter
from hnb.providers.demo.adapter import DemoAdapter
from hnb.providers.matter.adapter import MatterAdapter

log = logging.getLogger("hnb.providers")

BUILTIN: dict[str, type[ProviderAdapter]] = {
    DemoAdapter.provider_id: DemoAdapter,
    MatterAdapter.provider_id: MatterAdapter,
}
ADAPTERS = BUILTIN                     # back-compat name used by tests
PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
DEFAULT_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins"
ENTRY_POINT_GROUP = "hnb.providers"


@dataclass(frozen=True)
class LoadError:
    source: str
    reason: str


def check_adapter(cls: object) -> str | None:
    """Why `cls` can't be used as a provider, or None if it can."""
    if not isinstance(cls, type) or not issubclass(cls, ProviderAdapter):
        return "ADAPTER is not a ProviderAdapter subclass"
    pid = getattr(cls, "provider_id", None)
    if not isinstance(pid, str) or not PROVIDER_ID.match(pid):
        return "provider_id must be 2–32 lowercase letters, digits or _ (starting with a letter)"
    if not getattr(cls, "display_name", ""):
        return "display_name is required"
    abstract = getattr(cls, "__abstractmethods__", ())
    if abstract:
        return "missing methods: " + ", ".join(sorted(abstract))
    return None


def _plugin_dirs() -> list[Path]:
    dirs = [DEFAULT_PLUGIN_DIR]
    extra = os.environ.get("HNB_PLUGIN_DIRS", "")
    dirs += [Path(p) for p in extra.split(os.pathsep) if p.strip()]
    return dirs


def _load_plugin_file(path: Path) -> type:
    name = f"hnb_plugin_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
    if spec is None or spec.loader is None:
        raise ImportError("not importable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module                     # dataclasses & relative imports need it registered
    spec.loader.exec_module(module)
    if not hasattr(module, "ADAPTER"):
        raise ImportError("adapter.py defines no ADAPTER")
    return module.ADAPTER


def load_adapters(plugin_dirs: list[Path] | None = None, *, entry_points: bool = True
                  ) -> tuple[dict[str, type[ProviderAdapter]], list[LoadError]]:
    adapters: dict[str, type[ProviderAdapter]] = dict(BUILTIN)
    errors: list[LoadError] = []

    def add(cls: object, source: str) -> None:
        why = check_adapter(cls)
        if why:
            errors.append(LoadError(source, why))
            return
        pid = cls.provider_id                       # type: ignore[attr-defined]
        if pid in adapters:
            errors.append(LoadError(source, f"provider id {pid!r} is already taken"))
            return
        adapters[pid] = cls                         # type: ignore[assignment]

    if entry_points:
        try:
            eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except Exception:
            eps = []
        for ep in eps:
            try:
                add(ep.load(), f"entry point {ep.name}")
            except Exception as e:
                errors.append(LoadError(f"entry point {ep.name}", f"{type(e).__name__}: {e}"))

    for d in plugin_dirs if plugin_dirs is not None else _plugin_dirs():
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            f = sub / "adapter.py"
            if not sub.is_dir() or sub.name.startswith(("_", ".")) or not f.is_file():
                continue
            try:
                add(_load_plugin_file(f), f"plugins/{sub.name}")
            except Exception as e:
                errors.append(LoadError(f"plugins/{sub.name}", f"{type(e).__name__}: {e}"))
    for e in errors:
        log.warning("Provider module not loaded (%s): %s", e.source, e.reason)
    return adapters, errors
