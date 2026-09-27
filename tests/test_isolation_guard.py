"""Isolation guard: the product template must carry no one's real home.

Two tiers:

1. Generic checks, always on (CI, VM, any clone): private host addresses and
   narrow subnets, MAC addresses, tailnet names, credential shapes, key and
   certificate files, personal user-profile paths, and any
   image/video not explicitly allowlisted (personal imagery).

2. A private denylist that lives OUTSIDE the repository — never committed,
   because it holds the very identifiers it guards against (hostnames,
   volume/backup paths, device ids). Plain lines are case-insensitive terms;
   `sha256:<hex>` lines are hashes of secrets, matched against every token so
   the secret itself is never written down. Findings print the entry's line
   number, never its value.

   Location: $HNB_PRIVATE_DENYLIST, else ~/.hnb-starter/private-denylist.txt
   Packaging sets HNB_REQUIRE_DENYLIST=1: a missing or empty denylist FAILS.
"""
import hashlib
import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest

from hnb.security.redact import VENDOR_SHAPES

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
ALLOWED_MEDIA = ROOT / "tests" / "allowed-media.txt"

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?![\d.])")
MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b|\b[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}\b")
TAILNET = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b", re.I)
CERT_BLOCK = re.compile(r"-----BEGIN (?:CERTIFICATE|[A-Z ]*PRIVATE KEY)-----")
PROFILE_PATH = re.compile(r"\b[A-Za-z]:\\+Users\\+([^\\\s\"'`<>]+)|/home/([a-z_][a-z0-9_-]*)/")
PROFILE_OK = {"<you>", "you", "user", "username", "example", "public", "default", "debian", "runner", "hnb"}
SECRET_FILES = re.compile(r"(?i)(^|/)(\.env(\.(?!example$)[^/]+)?|[^/]*\.(pem|crt|cer|der|key|p12|pfx|jks|keystore)|id_(rsa|ed25519|ecdsa))$")
# Real data files: databases, backups, pre-restore snapshots, cookie jars, the private denylist itself.
DATA_FILES = re.compile(r"(?i)(^|/)([^/]*\.(db|sqlite3?|db-wal|db-shm|db-journal)|hnb-backup-[^/]*\.tar\.gz"
                        r"|pre-restore-[^/]*|[^/]*cookies?[^/]*\.txt|[^/]*private-denylist[^/]*)$")
# Paths that only exist on a developer's machine: agent worktrees and scratchpads, personal cloud folders.
WORKSPACE_PATHS = re.compile(r"\.claude[\\/]+(?:worktrees|projects)[\\/]|AppData[\\/]+Local[\\/]+Temp[\\/]+claude[\\/]"
                             r"|OneDrive[\\/]", re.I)
# A saved session: a curl cookie jar, or the hub's session cookie with a value.
SESSION_COOKIE = re.compile(r"# Netscape HTTP Cookie File|hnb_session(?:=|\s+)[A-Za-z0-9_\-]{20,}")
MEDIA = re.compile(r"(?i)\.(jpe?g|png|gif|webp|heic|heif|bmp|tiff?|mp4|mov|m4v|avi|mkv)$")
TOKEN = re.compile(r"[A-Za-z0-9_\-+/=.]{12,}")

TEXT_SUFFIXES = {".py", ".md", ".json", ".html", ".css", ".js", ".mjs", ".ts", ".tsx", ".toml", ".txt", ".sql",
                 ".yml", ".yaml", ".sh", ".cfg", ".ini", ".env", ".service", ".timer", ".example", ".lock", ""}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", ".playwright-mcp"}


def repo_paths() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [p for p in out.splitlines() if (ROOT / p).is_file()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Exported tarball (git archive): no repository, walk the tree.
        return [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def _private_host(ip: str, prefix: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not (addr.is_private or addr in ipaddress.ip_network("100.64.0.0/10")) or addr.is_loopback \
            or addr.is_unspecified or addr.is_reserved:
        return False
    if addr in ipaddress.ip_network("192.0.2.0/24") or addr in ipaddress.ip_network("198.51.100.0/24") \
            or addr in ipaddress.ip_network("203.0.113.0/24"):
        return False                                      # documentation ranges
    if prefix is not None:
        try:
            net = ipaddress.ip_network(f"{ip}/{prefix}", strict=True)
        except ValueError:
            return True
        return net.prefixlen > 16                         # a /8 or /12 is a standard; a /24 is someone's LAN
    return True


def scan_text(text: str) -> list[str]:
    hits = []
    if any(_private_host(m.group(1), m.group(2)) for m in IPV4.finditer(text)):
        hits.append("private IP or home subnet")
    if MAC.search(text):
        hits.append("MAC address")
    if TAILNET.search(text):
        hits.append("tailnet name")
    if VENDOR_SHAPES.search(text) or CERT_BLOCK.search(text):
        hits.append("secret or certificate material")
    for m in PROFILE_PATH.finditer(text):
        who = (m.group(1) or m.group(2) or "").strip().lower()
        if who not in PROFILE_OK:
            hits.append("personal user-profile path")
            break
    if WORKSPACE_PATHS.search(text):
        hits.append("agent workspace or personal cloud path")
    if SESSION_COOKIE.search(text):
        hits.append("session cookie")
    return hits


def scan_path(rel: str, allowed_media: set[str]) -> list[str]:
    hits = []
    if SECRET_FILES.search(rel):
        hits.append("secret-bearing file type")
    if DATA_FILES.search(rel):
        hits.append("database, backup or cookie file")
    if MEDIA.search(rel) and rel not in allowed_media:
        hits.append("image/video not in tests/allowed-media.txt (personal imagery?)")
    return hits


def load_denylist() -> tuple[Path, list[tuple[int, str]], set[str]]:
    path = Path(os.environ.get("HNB_PRIVATE_DENYLIST") or Path.home() / ".hnb-starter" / "private-denylist.txt")
    terms, hashes = [], set()
    if path.is_file():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("sha256:"):
                hashes.add(line[7:].strip().lower())
            elif len(line) >= 4:
                terms.append((n, line.lower()))
    return path, terms, hashes


def scan_denylist(text: str, terms, hashes) -> list[str]:
    low = text.lower()
    hits = [f"private denylist entry (line {n})" for n, term in terms
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", low)]
    if hashes:
        for tok in set(TOKEN.findall(text)):
            if hashlib.sha256(tok.encode()).hexdigest() in hashes:
                hits.append("private denylist secret hash")
                break
    return hits


def test_repo_has_files_to_scan():
    assert len(repo_paths()) >= 20, "guard would pass vacuously"


def test_no_real_home_identifiers_in_repo():
    allowed_media = set()
    if ALLOWED_MEDIA.is_file():
        allowed_media = {l.strip() for l in ALLOWED_MEDIA.read_text().splitlines() if l.strip() and not l.startswith("#")}
    _, terms, hashes = load_denylist()
    hits = []
    for rel in repo_paths():
        path = ROOT / rel
        if path.resolve() == SELF:
            continue
        found = scan_path(rel, allowed_media)
        if path.suffix in TEXT_SUFFIXES or path.name in ("Dockerfile", "install.sh") or rel.startswith("bin/"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            found += scan_text(text) + scan_denylist(text, terms, hashes)
        hits += [f"{rel}: {label}" for label in found]
    assert not hits, "\n".join(hits)


def test_private_denylist_is_present_when_packaging():
    if os.environ.get("HNB_REQUIRE_DENYLIST") != "1":
        pytest.skip("packaging mode off (set HNB_REQUIRE_DENYLIST=1)")
    path, terms, hashes = load_denylist()
    assert path.is_file() and (terms or hashes), f"private denylist missing or empty at {path}"


# ── controls: every detector must be able to fail, and must not cry wolf ──
@pytest.mark.parametrize("sample,label", [
    ("lamp at 192.168.1.20", "private IP or home subnet"),
    ("gw 10.0.0.1", "private IP or home subnet"),
    ("lan 192.168.4.0/24", "private IP or home subnet"),
    ("tailscale 100.101.102.103", "private IP or home subnet"),
    ("mac aa:bb:cc:dd:ee:ff", "MAC address"),
    ("mac AA-BB-CC-DD-EE-FF", "MAC address"),
    ("host box.tail1a2b3c.ts.net", "tailnet name"),
    ("token ghp_" + "a" * 36, "secret or certificate material"),
    ("-----BEGIN CERTIFICATE-----", "secret or certificate material"),
    (r"C:\Users\somebody\Documents\x", "personal user-profile path"),
    ("/home/somebody/brain/data", "personal user-profile path"),
    ("see D:/OneDrive/Documents/notes", "agent workspace or personal cloud path"),
    ("run from repo/.claude/worktrees/agent-1/plugins", "agent workspace or personal cloud path"),
    (r"saved to AppData\Local\Temp\claude\session\scratchpad", "agent workspace or personal cloud path"),
    ("# Netscape HTTP Cookie File", "session cookie"),
    ("Cookie: hnb_session=" + "Zq9" * 10, "session cookie"),
    ("127.0.0.1\tFALSE\t/\tFALSE\t0\thnb_session\t" + "x7" * 16, "session cookie"),
])
def test_guard_catches(sample, label):
    assert label in scan_text(sample)


@pytest.mark.parametrize("sample", [
    "docs example 192.0.2.10 and 203.0.113.9",
    "standard ranges 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16",
    "loopback 127.0.0.1 and 0.0.0.0",
    "commit 3da350c1f0e9d8c7b6a5",
    "uuid 2070ec75-73e4-4946-9ca7-ceb2fbb7b92b",
    "version 10.2.3",
    "path C:\\Users\\<you>\\HOME_NETWORK_BRAIN_STARTER and /home/debian/starter",
    "the session cookie is named hnb_session; .claude/ is git-ignored",
])
def test_guard_ignores_benign(sample):
    assert scan_text(sample) == []


@pytest.mark.parametrize("rel,bad", [
    ("certs/tv.pem", True), ("deploy/.env", True), ("x/vault.key", True), ("id_ed25519", True),
    ("var/data/hnb.db", True), ("hnb.db-wal", True), ("prod.sqlite3", True),
    ("backups/hnb-backup-20260926T041410Z.tar.gz", True), ("backups/pre-restore-20260926.db", True),
    ("evidence/cookies.txt", True), ("private-denylist.txt", True),
    ("deploy/hnb.env.example", False), ("web/app.js", False), ("hnb/store/db.py", False),
    ("tests/test_backup.py", False),
])
def test_guard_catches_secret_files(rel, bad):
    assert bool(scan_path(rel, set())) is bad


def test_guard_catches_unlisted_media():
    assert scan_path("web/img/front-door.jpg", set())
    assert not scan_path("web/img/front-door.jpg", {"web/img/front-door.jpg"})


def test_denylist_terms_and_hashes_match_without_revealing(tmp_path):
    secret = "q7-" + "Zx9" * 8
    terms = [(3, "examplehost-01")]
    hashes = {hashlib.sha256(secret.encode()).hexdigest()}
    hits = scan_denylist(f"ssh examplehost-01 with key {secret}", terms, hashes)
    assert hits == ["private denylist entry (line 3)", "private denylist secret hash"]
    assert all("examplehost" not in h and secret not in h for h in hits)
    assert scan_denylist("examplehost-012 is a different machine", terms, set()) == []
