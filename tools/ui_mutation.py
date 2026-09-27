"""Prove the browser tests are load-bearing: undo one UI fix at a time and
check that tests/test_web_browser.py fails without it.

    python tools/ui_mutation.py

Needs what the browser tests need (`pip install -e ".[browser]"` and a
Chromium). Every file is restored afterwards, even on error or Ctrl-C.
Run it on a working tree nothing else is serving from.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS, CSS = ROOT / "web" / "app.js", ROOT / "web" / "app.css"

# name: (file, text that must be present, what to replace it with)
MUTATIONS = {
    "locks/climate section not rendered": (JS, '  ${section("Locks, climate &amp; more", "more", moreCard, true, false)}', ""),
    "favourites fall through to the camera card": (JS, 'd.section === "cameras" ? camCard(d) : moreCard(d)', "camCard(d)"),
    "switch hit area under 44 px": (CSS, '.toggle::before { content: ""; position: absolute; inset: -5px -2px; border-radius: 999px; }', ""),
    "sliders 32 px tall": (CSS, "accent-color: var(--signal); height: 44px; }", "accent-color: var(--signal); height: 32px; }"),
    "two home fetches per refresh": (JS, '    if (!["home", "rooms"].includes(r)) await loadHome();', "    await loadHome();"),
    "hero says 0 lights on when all are unknown": (JS, "${st.lights_on || !st.lights_unknown ?", "${true ?"),
    "raw JSON in the activity log": (JS, 'case "THERMOSTAT": return v && typeof v === "object"', 'case "THERMOSTAT_": return v && typeof v === "object"'),
    "room renames missing from the activity log": (JS, 'e.action === "ROOM_RENAMED" && e.data && e.data.before', "false"),
    "no room editor": (JS, '${S.editRooms ? `<div class="card">${roomEditor()}</div>` : ""}', ""),
}


def main() -> int:
    missed = []
    for name, (path, present, replacement) in MUTATIONS.items():
        original = path.read_text(encoding="utf-8")
        if original.count(present) != 1:
            print(f"STALE    {name}: the guarded code changed — update this mutation")
            missed.append(name)
            continue
        try:
            path.write_text(original.replace(present, replacement, 1), encoding="utf-8", newline="\n")
            r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_web_browser.py", "-q", "-x",
                                "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, timeout=900)
            caught = r.returncode == 1          # 1 = tests failed; 0 (or a skipped suite) = the fix isn't guarded
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")
        print(f"{'CAUGHT' if caught else 'MISSED':8} {name}", flush=True)
        if not caught:
            missed.append(name)
    print(f"{len(MUTATIONS) - len(missed)} of {len(MUTATIONS)} caught")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
