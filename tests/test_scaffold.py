"""`hnb new-provider` must produce a module that passes the conformance kit as-is
and can sit next to the example (and other modules) without breaking pytest's
collection — plugin folders aren't packages, so test files need unique names."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hnb.cli import ScaffoldError, scaffold_provider
from hnb.providers.registry import load_adapters

ROOT = Path(__file__).resolve().parents[1]


def test_scaffold_loads_and_passes_the_kit_next_to_the_example(tmp_path):
    dest, cls = scaffold_provider("my_switches", "My switches", tmp_path / "plugins")
    assert cls == "MySwitchesAdapter"
    assert (dest / "test_my_switches.py").is_file() and not (dest / "test_conformance.py").exists()
    text = (dest / "adapter.py").read_text(encoding="utf-8")
    assert 'provider_id = "my_switches"' in text and 'display_name = "My switches"' in text
    assert "Acme smart plugs" not in text and "class MySwitchesAdapter(" in text
    for f in ("fake_device.py", "test_my_switches.py"):                  # no stale pointers to the old name
        assert "test_conformance.py" not in (dest / f).read_text(encoding="utf-8")
    for f in ("fake_device.py", "adapter.py"):                             # its own simulator port
        assert "7788" not in (dest / f).read_text(encoding="utf-8")

    adapters, errors = load_adapters([tmp_path / "plugins"], entry_points=False)
    assert "my_switches" in adapters and errors == []

    # The repo on PYTHONPATH: CI (and a git archive export) run without `pip install -e`.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))}
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                        str(ROOT / "examples" / "providers" / "acme_plug"), str(dest)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "24 passed" in r.stdout                      # 12 for the example + 12 for the scaffold


@pytest.mark.parametrize("bad", ["x", "1abc", "has-dash", "UPPER", "demo", "matter"])
def test_scaffold_refuses_bad_or_builtin_ids(tmp_path, bad):
    with pytest.raises(ScaffoldError):
        scaffold_provider(bad, None, tmp_path)


def test_scaffold_never_overwrites(tmp_path):
    scaffold_provider("my_plugs", None, tmp_path)
    with pytest.raises(ScaffoldError, match="already exists"):
        scaffold_provider("my_plugs", None, tmp_path)
