-- Esquema relacional de SMURF.
-- SQLite con WAL. Cada tabla incluye created_at/updated_at en epoch.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin',  -- superadmin|admin|supervisor|user
    email           TEXT,
    totp_secret     TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS extensions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL DEFAULT '',
    sip_password    TEXT NOT NULL,
    ha1_md5         TEXT NOT NULL DEFAULT '',
    ha1_sha256      TEXT NOT NULL DEFAULT '',
    email           TEXT,
    voicemail_pin   TEXT,
    voicemail_enabled INTEGER NOT NULL DEFAULT 1,
    forward_busy    TEXT,
    forward_noanswer TEXT,
    forward_unconditional TEXT,
    no_answer_seconds INTEGER NOT NULL DEFAULT 25,
    max_concurrent_calls INTEGER NOT NULL DEFAULT 5,
    record_calls    INTEGER NOT NULL DEFAULT 0,
    pickup_group    TEXT NOT NULL DEFAULT 'default',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS trunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    host            TEXT NOT NULL,
    port            INTEGER NOT NULL DEFAULT 5060,
    transport       TEXT NOT NULL DEFAULT 'udp',
    username        TEXT,
    password        TEXT,
    realm           TEXT,
    auth_mode       TEXT NOT NULL DEFAULT 'credentials',  -- credentials|ip
    register        INTEGER NOT NULL DEFAULT 1,
    register_expires INTEGER NOT NULL DEFAULT 3600,
    from_user       TEXT,
    from_domain     TEXT,
    outbound_proxy  TEXT,
    failover_to     INTEGER REFERENCES trunks(id) ON DELETE SET NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS dial_plan (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    direction       TEXT NOT NULL,         -- inbound|outbound|internal
    pattern         TEXT NOT NULL,         -- regex
    target_type     TEXT NOT NULL,         -- extension|queue|ivr|ringgroup|trunk|voicemail|hangup|conference
    target_value    TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    cli_prefix      TEXT,
    strip_digits    INTEGER NOT NULL DEFAULT 0,
    prepend         TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS ring_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    strategy        TEXT NOT NULL DEFAULT 'ringall',  -- ringall|hunt|random
    ring_seconds    INTEGER NOT NULL DEFAULT 25,
    members_csv     TEXT NOT NULL DEFAULT '',
    no_answer_dest  TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS queues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    strategy        TEXT NOT NULL DEFAULT 'roundrobin',  -- roundrobin|leastrecent|random|priority
    timeout         INTEGER NOT NULL DEFAULT 600,
    max_wait        INTEGER NOT NULL DEFAULT 300,
    moh_class       TEXT NOT NULL DEFAULT 'default',
    members_csv     TEXT NOT NULL DEFAULT '',
    no_answer_dest  TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ivrs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    greeting        TEXT NOT NULL DEFAULT '',
    timeout         INTEGER NOT NULL DEFAULT 5,
    invalid_dest    TEXT,
    timeout_dest    TEXT,
    options_json    TEXT NOT NULL DEFAULT '{}'  -- {"1":"ext:1001","2":"queue:2000"}
);

CREATE TABLE IF NOT EXISTS schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    rules_json      TEXT NOT NULL DEFAULT '[]',  -- [{"days":"Mon-Fri","from":"09:00","to":"18:00"}]
    open_dest       TEXT,
    closed_dest     TEXT
);

CREATE TABLE IF NOT EXISTS dids (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL UNIQUE,
    trunk_id        INTEGER REFERENCES trunks(id) ON DELETE SET NULL,
    target_type     TEXT NOT NULL,
    target_value    TEXT NOT NULL,
    cnam            TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS conferences (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    pin             TEXT,
    admin_pin       TEXT,
    moh_on_join     INTEGER NOT NULL DEFAULT 1,
    max_members     INTEGER NOT NULL DEFAULT 50,
    record          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cdr (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         TEXT NOT NULL,
    started_at      REAL NOT NULL,
    answered_at     REAL,
    ended_at        REAL,
    src_number      TEXT,
    src_name        TEXT,
    dst_number      TEXT,
    dst_name        TEXT,
    direction       TEXT,
    disposition     TEXT,                   -- ANSWERED|NO_ANSWER|BUSY|FAILED|CANCELLED
    duration        INTEGER NOT NULL DEFAULT 0,
    bill_seconds    INTEGER NOT NULL DEFAULT 0,
    via_trunk       TEXT,
    recording_path  TEXT,
    hangup_cause    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr(started_at);
CREATE INDEX IF NOT EXISTS idx_cdr_call ON cdr(call_id);

CREATE TABLE IF NOT EXISTS voicemail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    extension       TEXT NOT NULL,
    caller          TEXT,
    received_at     REAL NOT NULL DEFAULT (strftime('%s','now')),
    duration        INTEGER NOT NULL DEFAULT 0,
    file_path       TEXT NOT NULL,
    seen            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vm_ext ON voicemail(extension);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src             TEXT NOT NULL,
    dst             TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         REAL NOT NULL DEFAULT (strftime('%s','now')),
    delivered       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_dst ON chat_messages(dst, sent_at);

CREATE TABLE IF NOT EXISTS blacklist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL,
    note            TEXT,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_bl_number ON blacklist(number);

CREATE TABLE IF NOT EXISTS banned_ips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ip              TEXT NOT NULL UNIQUE,
    banned_at       REAL NOT NULL DEFAULT (strftime('%s','now')),
    until           REAL NOT NULL,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS settings_kv (
    k               TEXT PRIMARY KEY,
    v               TEXT NOT NULL,
    updated_at      REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS provisioning_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mac             TEXT NOT NULL UNIQUE,
    vendor          TEXT NOT NULL,
    model           TEXT,
    extension       TEXT,
    notes           TEXT,
    last_seen       REAL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    created_at      REAL NOT NULL DEFAULT (strftime('%s','now')),
    last_used_at    REAL
);

CREATE TABLE IF NOT EXISTS webhooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    events_csv      TEXT NOT NULL DEFAULT '',  -- call.start,call.answered,call.end,voicemail.new
    secret          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);
