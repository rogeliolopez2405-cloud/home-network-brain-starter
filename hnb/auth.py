"""Owner authentication.

First-owner bootstrap
    Open only while no owner exists, only from the home network (enforced by
    the network policy for every request), and only with the one-time setup
    code the hub writes to its own data directory on first start. The
    installer never prints the code; the owner reads it on the hub with
    `sudo bin/setup-code`. Creating the owner deletes the code and closes
    bootstrap permanently (and a DB trigger refuses a second owner).

Passphrases
    argon2id (RFC 9106 low-memory profile: t=3, m=64 MiB, p=4), NFC-normalised,
    10–256 characters. Hashing runs off the event loop. Unknown usernames are
    verified against a dummy hash so timing does not reveal who exists.
    Hashes are upgraded on login when parameters change.

Sessions
    256-bit random token in an HttpOnly, SameSite=Strict cookie; only its
    SHA-256 is stored. Idle and absolute expiry. Every write also needs the
    session's CSRF token in the X-CSRF-Token header. The token is never
    returned in a response body.

Rate limiting
    Failed logins/bootstraps per client address, sliding window, bounded map.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from hnb.core.model import AuditAction, HistoryEvent, House, new_id
from hnb.security.redact import REDACTOR
from hnb.store.db import tx
from hnb.store.repo import Repo

PASS_MIN, PASS_MAX = 10, 256
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$")
SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"    # no 0/O, 1/I
FAILURE = "That name and passphrase don't match."


class AuthError(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


@dataclass(frozen=True)
class Session:
    token_hash: str
    owner_id: str
    username: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class NewSession:
    token: str            # goes into the cookie only
    csrf_token: str
    username: str
    max_age: int


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_pass(p: str) -> str:
    return unicodedata.normalize("NFC", p or "")


def normalize_setup_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


class RateLimiter:
    """At most `limit` failures per `window_s` per key; bounded memory."""

    def __init__(self, limit: int = 5, window_s: float = 900, max_keys: int = 4096,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.max_keys, self.clock = limit, window_s, max_keys, clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str) -> deque[float]:
        q = self._hits.get(key, deque())
        cutoff = self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int | None:
        q = self._prune(key)
        if len(q) >= self.limit:
            return max(1, int(q[0] + self.window - self.clock()))
        return None

    def fail(self, key: str) -> None:
        q = self._prune(key)
        q.append(self.clock())
        self._hits[key] = q
        self._hits.move_to_end(key)
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class AuthService:
    def __init__(self, conn: sqlite3.Connection, setup_code_file: Path, *,
                 idle_s: int, absolute_s: int, now: Callable[[], datetime] | None = None,
                 hasher: PasswordHasher | None = None):
        self.conn, self.repo = conn, Repo(conn)
        self.setup_code_file = Path(setup_code_file)
        self.idle, self.absolute = timedelta(seconds=idle_s), timedelta(seconds=absolute_s)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ph = hasher or PasswordHasher()
        self._dummy = self.ph.hash(secrets.token_urlsafe(16))
        self.limiter = RateLimiter()

    # ── state ────────────────────────────────────────────────────────────
    def owner_exists(self) -> bool:
        return self.conn.execute("SELECT EXISTS (SELECT 1 FROM owners)").fetchone()[0] == 1

    def setup_required(self) -> bool:
        return not self.owner_exists()

    def ensure_setup_code(self) -> bool:
        """Write a one-time setup code if setup is open and none exists.
        Returns True when setup is open. Never logs or returns the code."""
        if self.owner_exists():
            self._remove_setup_code()
            return False
        if not self.setup_code_file.exists():
            raw = "".join(secrets.choice(SETUP_ALPHABET) for _ in range(12))
            code = f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"
            self.setup_code_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.setup_code_file.with_name(f".{self.setup_code_file.name}.{os.getpid()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, (code + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.setup_code_file)
        return True

    def _remove_setup_code(self) -> None:
        try:
            self.setup_code_file.unlink()
        except FileNotFoundError:
            pass

    def _setup_code_ok(self, code: str) -> bool:
        try:
            expected = normalize_setup_code(self.setup_code_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        given = normalize_setup_code(code)
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    def _event(self, action: AuditAction, actor: str, *, target: str | None = None, result: str | None = None,
               client: str | None = None, **data) -> HistoryEvent:
        if client:
            data["client"] = client
        return HistoryEvent(action=action, actor=actor, source="api", target=target, result=result, data=data)

    def _limit(self, client: str) -> None:
        wait = self.limiter.retry_after(client)
        if wait:
            raise AuthError(429, f"Too many attempts. Try again in {wait // 60 + 1} minutes.", wait)

    # ── bootstrap ────────────────────────────────────────────────────────
    async def bootstrap(self, *, setup_code: str, username: str, passphrase: str, house_name: str,
                        client: str) -> NewSession:
        self._limit(client)
        if self.owner_exists():
            raise AuthError(409, "This hub already has an owner. Sign in instead.")
        if not self._setup_code_ok(setup_code):
            self.limiter.fail(client)
            with tx(self.conn):
                self.repo.append_events([self._event(AuditAction.LOGIN_FAILED, "anonymous", result="BAD_SETUP_CODE",
                                                     client=client, stage="bootstrap")])
            raise AuthError(403, "That setup code isn't right. Read it on the hub with: sudo /opt/hnb/bin/setup-code")
        username = (username or "").strip()
        if not USERNAME.match(username):
            raise AuthError(400, "Use 2–40 letters, numbers, dots, dashes or underscores for the sign-in name.")
        pw = _norm_pass(passphrase)
        if not PASS_MIN <= len(pw) <= PASS_MAX:
            raise AuthError(400, f"Use a passphrase of {PASS_MIN}–{PASS_MAX} characters.")
        house_name = " ".join((house_name or "").split())[:60] or "My Home"
        pass_hash = await asyncio.to_thread(self.ph.hash, pw)

        owner_id = new_id()
        try:
            with tx(self.conn):
                if self.conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]:
                    raise AuthError(409, "This hub already has an owner. Sign in instead.")
                self.conn.execute("INSERT INTO owners (id, username, pass_hash, created_at) VALUES (?, ?, ?, ?)",
                                  (owner_id, username, pass_hash, self.now().isoformat()))
                house = self.repo.load_house() or House(name=house_name)
                house.name = house_name
                self.repo.save_house(house)
                self.repo.kv_set("bootstrap_closed_at", self.now().isoformat())
                self.repo.append_events([self._event(AuditAction.OWNER_CREATED, f"owner:{username}",
                                                     target=owner_id, result="CREATED", client=client,
                                                     house=house_name)])
                session = self._create_session(owner_id, username)
        except sqlite3.IntegrityError:
            raise AuthError(409, "This hub already has an owner. Sign in instead.") from None
        self._remove_setup_code()
        self.limiter.reset(client)
        return session

    # ── login / logout ───────────────────────────────────────────────────
    async def login(self, *, username: str, passphrase: str, client: str) -> NewSession:
        self._limit(client)
        row = self.conn.execute("SELECT id, username, pass_hash FROM owners WHERE username = ?",
                                ((username or "").strip(),)).fetchone()
        pw = _norm_pass(passphrase)[:PASS_MAX * 4]
        ok = await asyncio.to_thread(self._verify, row["pass_hash"] if row else self._dummy, pw)
        if not (row and ok):
            self.limiter.fail(client)
            with tx(self.conn):
                self.repo.append_events([self._event(AuditAction.LOGIN_FAILED, "anonymous", result="FAILED",
                                                     client=client, username=(username or "")[:40])])
            raise AuthError(401, FAILURE)
        self.limiter.reset(client)
        new_hash = None
        if self.ph.check_needs_rehash(row["pass_hash"]):
            new_hash = await asyncio.to_thread(self.ph.hash, pw)
        with tx(self.conn):
            now = self.now().isoformat()
            if new_hash:
                self.conn.execute("UPDATE owners SET pass_hash = ? WHERE id = ?", (new_hash, row["id"]))
            self.conn.execute("UPDATE owners SET last_login_at = ? WHERE id = ?", (now, row["id"]))
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self.repo.append_events([self._event(AuditAction.LOGIN_SUCCEEDED, f"owner:{row['username']}",
                                                 target=row["id"], result="OK", client=client)])
            return self._create_session(row["id"], row["username"])

    def _verify(self, pass_hash: str, pw: str) -> bool:
        try:
            return self.ph.verify(pass_hash, pw)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def _create_session(self, owner_id: str, username: str) -> NewSession:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.now()
        self.conn.execute(
            "INSERT INTO sessions (token_hash, owner_id, csrf_token, created_at, last_seen_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_hash_token(token), owner_id, csrf, now.isoformat(), now.isoformat(), (now + self.absolute).isoformat()))
        REDACTOR.register(token)
        return NewSession(token=token, csrf_token=csrf, username=username, max_age=int(self.absolute.total_seconds()))

    def logout(self, session: Session, client: str) -> None:
        with tx(self.conn):
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))
            self.repo.append_events([self._event(AuditAction.LOGOUT, f"owner:{session.username}",
                                                 target=session.owner_id, result="OK", client=client)])

    # ── session resolution ───────────────────────────────────────────────
    def resolve(self, token: str | None) -> Session | None:
        if not token or len(token) > 200:
            return None
        th = _hash_token(token)
        row = self.conn.execute(
            "SELECT s.token_hash, s.owner_id, s.csrf_token, s.last_seen_at, s.expires_at, o.username "
            "FROM sessions s JOIN owners o ON o.id = s.owner_id WHERE s.token_hash = ?", (th,)).fetchone()
        if row is None:
            return None
        now = self.now()
        expires = datetime.fromisoformat(row["expires_at"])
        last_seen = datetime.fromisoformat(row["last_seen_at"])
        if now >= expires or now - last_seen >= self.idle:
            with tx(self.conn):
                self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            return None
        if now - last_seen > timedelta(seconds=60):    # throttle writes
            with tx(self.conn):
                self.conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now.isoformat(), th))
        return Session(token_hash=th, owner_id=row["owner_id"], username=row["username"],
                       csrf_token=row["csrf_token"], expires_at=expires)

    @staticmethod
    def csrf_ok(session: Session, header: str | None) -> bool:
        return bool(header) and hmac.compare_digest(header.encode(), session.csrf_token.encode())
