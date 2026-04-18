-- SMURF PBX — configuration and runtime state (PostgreSQL)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS extensions (
    id              BIGSERIAL PRIMARY KEY,
    number          VARCHAR(32) NOT NULL UNIQUE,
    secret          TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    max_concurrent  INT NOT NULL DEFAULT 4 CHECK (max_concurrent > 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_users (
    id            BIGSERIAL PRIMARY KEY,
    username      VARCHAR(64) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          VARCHAR(32) NOT NULL DEFAULT 'superadmin',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS registrations (
    extension   VARCHAR(32) PRIMARY KEY REFERENCES extensions (number) ON DELETE CASCADE,
    aor         TEXT NOT NULL,
    contact_uri TEXT NOT NULL,
    remote_ip   INET NOT NULL,
    remote_port INT NOT NULL CHECK (remote_port > 0 AND remote_port < 65536),
    transport   VARCHAR(8) NOT NULL CHECK (transport IN ('udp', 'tcp', 'tls')),
    expires_at  TIMESTAMPTZ NOT NULL,
    call_id     TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_registrations_expires ON registrations (expires_at);

CREATE TABLE IF NOT EXISTS cdr (
    id           BIGSERIAL PRIMARY KEY,
    call_id      TEXT NOT NULL,
    from_ext     VARCHAR(32),
    to_ext       VARCHAR(32),
    direction    VARCHAR(16) NOT NULL DEFAULT 'internal',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at  TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    duration_sec INT,
    hangup_cause TEXT
);

CREATE INDEX IF NOT EXISTS idx_cdr_started ON cdr (started_at DESC);
