"""CLI behaviour a developer meets first: `setup-code` after `dev`."""
from pathlib import Path

import pytest

from hnb import cli


@pytest.fixture
def clean_env(monkeypatch):
    # setenv (not delenv) so monkeypatch restores the original — absent — values afterwards,
    # even though the command itself sets HNB_DEV.
    monkeypatch.setenv("HNB_DATA_DIR", "")
    monkeypatch.setenv("HNB_DEV", "")
    if Path("/var/lib/hnb/data").exists():
        pytest.skip("an installed hub is present on this machine")


def test_setup_code_finds_a_dev_hub_in_var(tmp_path, monkeypatch, capsys, clean_env):
    """The documented path: `python -m hnb.cli dev`, then `setup-code` in another terminal."""
    code = tmp_path / "var" / "data" / "setup-code"
    code.parent.mkdir(parents=True)
    code.write_text("ABCD-EFGH-JKLM\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["setup-code"]) == 0
    assert capsys.readouterr().out.strip() == "ABCD-EFGH-JKLM"


def test_setup_code_names_the_folder_it_looked_in(tmp_path, monkeypatch, capsys, clean_env):
    monkeypatch.setenv("HNB_DATA_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["setup-code"]) == 1
    err = capsys.readouterr().err
    assert "elsewhere" in err and "HNB_DATA_DIR" in err
