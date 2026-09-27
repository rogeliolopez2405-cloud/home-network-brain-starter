"""The committed API schema must match the code. Regenerate with
`python tools/gen_api_docs.py` after changing any endpoint."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_api_docs", ROOT / "tools" / "gen_api_docs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_api_docs"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_committed_openapi_matches_the_code():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    current = _gen().openapi_spec()
    assert committed == current, "docs/openapi.json is stale — run: python tools/gen_api_docs.py"


def test_api_md_documents_every_endpoint():
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    api_md = (ROOT / "API.md").read_text(encoding="utf-8")
    missing = [f"{m.upper()} {p}" for p, ops in spec["paths"].items() for m in ops if f"`{p}`" not in api_md]
    assert not missing, missing
