"""Regenerate requirements.lock (hash-pinned Linux wheels) from requirements.in.

    python tools/lock.py

Resolves with pip for CPython 3.13 on manylinux x86_64 and aarch64, binary
wheels only, and records every wheel's sha256. The container installs with
`pip install --require-hashes --only-binary=:all:`, so a changed or
substituted file on the index fails the build instead of shipping.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHES = ("x86_64", "aarch64")
PY = "3.13"


def resolve(arch: str, tmp: Path) -> list[dict]:
    report = tmp / f"report-{arch}.json"
    plats = [f"manylinux_2_28_{arch}", f"manylinux_2_17_{arch}", f"manylinux2014_{arch}"]
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
           "--report", str(report), "--python-version", PY, "--implementation", "cp",
           "--only-binary=:all:", "--target", str(tmp / "t"), "-r", str(ROOT / "requirements.in")]
    for p in plats:
        cmd += ["--platform", p]
    subprocess.run(cmd, check=True)
    return json.loads(report.read_text(encoding="utf-8"))["install"]


def main() -> int:
    pkgs: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as t:
        for arch in ARCHES:
            for item in resolve(arch, Path(t)):
                name, ver = item["metadata"]["name"].lower(), item["metadata"]["version"]
                info = item["download_info"]["archive_info"]
                sha = (info.get("hashes") or {}).get("sha256") or info["hash"].split("=", 1)[1]
                entry = pkgs.setdefault(name, {"version": ver, "hashes": set()})
                if entry["version"] != ver:
                    raise SystemExit(f"{name}: {entry['version']} vs {ver} across architectures")
                entry["hashes"].add(sha)
    lines = ["# Hash-pinned runtime dependencies for the hub container (Linux, CPython 3.13,",
             "# x86_64 + aarch64 wheels). Installed with: pip install --require-hashes --only-binary=:all:",
             "# Regenerate from requirements.in with: python tools/lock.py  (never edit hashes by hand)", ""]
    for name in sorted(pkgs):
        hashes = sorted(pkgs[name]["hashes"])
        lines.append(f"{name}=={pkgs[name]['version']} \\")
        lines += [f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "") for i, h in enumerate(hashes)]
    (ROOT / "requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(pkgs)} packages locked:", ", ".join(f"{n}=={pkgs[n]['version']}" for n in sorted(pkgs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
