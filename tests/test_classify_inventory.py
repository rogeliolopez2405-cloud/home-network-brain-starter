"""Inventory classifier, on a fictional inventory."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("classify_inventory", ROOT / "tools" / "classify_inventory.py")
ci = importlib.util.module_from_spec(spec)
sys.modules["classify_inventory"] = ci          # dataclasses need the module registered
spec.loader.exec_module(ci)

HEADER = "room,device_type,brand,model,controlled_by,in_alexa,matter_logo_or_code,radio,echo_model_if_echo,notes\n"
ROWS = """Living Room,light switch,Example Controls,SW-1,Alexa,yes,yes,wi-fi,,
Kitchen,smart plug,Example Power,P-2,Alexa,yes,yes,thread,,
Bedroom,smart bulb,,,Alexa,yes,no,zigbee,,paired to the Echo
Hall,dimmer,Lutron,Caseta PD-6WCL,Lutron app,yes,no,other,,
Office,smart plug,Meross,MSS110,Meross app,yes,no,wi-fi,,
Den,TV,Roku,Ultra,Roku app,no,no,wi-fi,,
Kitchen,speaker,Amazon,Echo Hub,Alexa,yes,,,Echo Hub,
Porch,light,,,Alexa,yes,,,,
"""


def test_every_category_and_the_first_pick(tmp_path):
    f = tmp_path / "inventory.csv"
    f.write_text(HEADER + ROWS, encoding="utf-8")
    rows = ci.classify_file(f)
    cats = [r.category for r in rows]
    assert cats == [ci.DIRECT, ci.DIRECT, ci.ALEXA, ci.VENDOR, ci.HA, ci.VENDOR, ci.ALEXA, ci.UNKNOWN]
    out = ci.report(rows)
    assert "Recommended first real device" in out
    first = out.split("## Recommended first real device")[1]
    assert "Example Controls" in first.splitlines()[2]            # Wi-Fi Matter switch beats Thread plug


def test_no_matter_device_says_so(tmp_path):
    f = tmp_path / "inventory.csv"
    f.write_text(HEADER + "Hall,dimmer,Lutron,Caseta,Lutron app,yes,no,other,,\n", encoding="utf-8")
    out = ci.report(ci.classify_file(f))
    assert "No Matter device found" in out and "VENDOR ADAPTER" in out


def test_refuses_an_inventory_inside_the_repo(tmp_path, capsys):
    inside = ROOT / "pilot" / "real-inventory.csv"
    assert ci.main(["x", str(inside)]) == 1
    assert "outside the repository" in capsys.readouterr().out
