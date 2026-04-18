-- Voicemail messages (PCMU deposit → WAV on disk)
CREATE TABLE IF NOT EXISTS voicemail_messages (
    id            BIGSERIAL PRIMARY KEY,
    mailbox_ext   VARCHAR(32) NOT NULL REFERENCES extensions (number) ON DELETE CASCADE,
    caller_ext    VARCHAR(64),
    file_path     TEXT NOT NULL,
    duration_ms   INT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    listened_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_vm_mailbox ON voicemail_messages (mailbox_ext, created_at DESC);

-- SIP trunks for outbound PSTN-style calls (REGISTER + INVITE with digest)
CREATE TABLE IF NOT EXISTS sip_trunks (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(64) NOT NULL UNIQUE,
    sip_host        TEXT NOT NULL,
    sip_port        INT NOT NULL DEFAULT 5060 CHECK (sip_port > 0 AND sip_port < 65536),
    transport       VARCHAR(8) NOT NULL DEFAULT 'udp' CHECK (transport IN ('udp', 'tcp', 'tls')),
    auth_username   TEXT NOT NULL DEFAULT '',
    auth_password   TEXT NOT NULL DEFAULT '',
    from_user       TEXT NOT NULL DEFAULT '',
    register_uri    TEXT NOT NULL DEFAULT '',
    contact_user    TEXT NOT NULL DEFAULT 'smurf',
    enabled         BOOLEAN NOT NULL DEFAULT true,
    priority        INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sip_trunks_priority ON sip_trunks (enabled, priority DESC, id);
