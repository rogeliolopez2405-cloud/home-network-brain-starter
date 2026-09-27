"""Release / packaging gate. A package is produced only if everything passes
from a clean `git archive` export with the private denylist REQUIRED.

    python tools/release_check.py            # check only
    python tools/release_check.py --package  # also write dist/hnb-starter-<version>.tar.gz

Fails if: the working tree has uncommitted changes, the export's tests fail,
or the isolation guard finds any of the owner's private identifiers (read from
~/.hnb-starter/private-denylist.txt, which is never committed).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd, **kw):
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def main() -> int:
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if dirty:
        print("Refusing: uncommitted changes. Package only what is committed.\n" + dirty)
        return 1
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="hnb-release-") as tmp:
        archive = Path(tmp) / "export.tar"
        run(["git", "archive", "-o", str(archive), "HEAD"], cwd=ROOT)
        export = Path(tmp) / "export"
        export.mkdir()
        with tarfile.open(archive) as t:
            t.extractall(export, filter="data")
        env = {**os.environ, "HNB_REQUIRE_DENYLIST": "1"}
        run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=export, env=env)
        if "--package" in sys.argv:
            version = next(l.split('"')[1] for l in (export / "hnb" / "__init__.py").read_text().splitlines()
                           if l.startswith("__version__"))
            dist = ROOT / "dist"
            dist.mkdir(exist_ok=True)
            out = dist / f"hnb-starter-{version}-{head}.tar.gz"
            run(["git", "archive", "--format=tar.gz", f"--prefix=hnb-starter-{version}/", "-o", str(out), "HEAD"], cwd=ROOT)
            print(f"Packaged {out.name}")
    print(f"Release check passed for {head}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
