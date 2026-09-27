-- 0001 — initial schema.
-- Applied inside one transaction by hnb.store.db.migrate(); never edit a
-- released migration, add a new numbered file instead.

CREATE TABLE kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE house (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    singleton   INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK (singleton = 1)
);

CREATE TABLE owners (
    id             TEXT PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pass_hash      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_login_at  TEXT
);
-- M1 is single-owner: bootstrap closes permanently once an owner exists.
CREATE TRIGGER owners_single_owner BEFORE INSERT ON owners
WHEN (SELECT COUNT(*) FROM owners) >= 1
BEGIN SELECT RAISE(ABORT, 'an owner already exists'); END;

CREATE TABLE sessions (
    token_hash    TEXT PRIMARY KEY,           -- sha256 of the cookie value; the token itself is never stored
    owner_id      TEXT NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
CREATE INDEX sessions_owner ON sessions(owner_id);

CREATE TABLE providers (
    provider_id   TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    config_json   TEXT NOT NULL DEFAULT '{}',  -- non-secret settings only
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at    TEXT NOT NULL
);

CREATE TABLE provider_secrets (
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    key_id       TEXT NOT NULL,               -- fingerprint of the vault key that sealed it
    nonce        BLOB NOT NULL,
    ciphertext   BLOB NOT NULL,               -- never plaintext
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (provider_id, name)
);

CREATE TABLE rooms (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    icon        TEXT NOT NULL,
    sort        INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE devices (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    room_id       TEXT REFERENCES rooms(id) ON DELETE SET NULL,
    review        TEXT NOT NULL CHECK (review IN ('UNREVIEWED', 'MINE', 'NOT_MINE', 'IGNORED')),
    favorite      INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    manufacturer  TEXT,
    model         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE connections (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
    external_id  TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (provider_id, external_id)          -- one provider handle = one device, ever
);
CREATE INDEX connections_device ON connections(device_id);

CREATE TABLE capabilities (
    device_id      TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    type           TEXT NOT NULL,
    connection_id  TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    readable       INTEGER NOT NULL,
    writable       INTEGER NOT NULL,
    options_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (device_id, type)
);

CREATE TABLE device_state (
    device_id          TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    capability         TEXT NOT NULL,
    value_json         TEXT,
    freshness          TEXT NOT NULL CHECK (freshness IN ('CONFIRMED', 'ASSUMED', 'STALE', 'UNKNOWN')),
    observed_at        TEXT,
    via_connection_id  TEXT REFERENCES connections(id) ON DELETE SET NULL,
    PRIMARY KEY (device_id, capability)
);

CREATE TABLE actions (
    id             TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    capability     TEXT NOT NULL,
    value_json     TEXT,
    actor          TEXT NOT NULL,
    source         TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('PENDING', 'ACCEPTED', 'CONFIRMED', 'FAILED', 'REJECTED')),
    detail         TEXT NOT NULL DEFAULT '',
    connection_id  TEXT,
    requested_at   TEXT NOT NULL,
    completed_at   TEXT
);
CREATE INDEX actions_device ON actions(device_id, requested_at);

CREATE TABLE audit_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    at              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT,
    requested_json  TEXT,
    result          TEXT,
    source          TEXT NOT NULL,
    data_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_at ON audit_log(at);
CREATE INDEX audit_target ON audit_log(target);

-- Append-only at the database level: no application path can rewrite history.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
