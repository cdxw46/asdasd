-- Call queues (hunt: sequential ring to members with active registration)
CREATE TABLE IF NOT EXISTS call_queues (
    slug             VARCHAR(64) PRIMARY KEY,
    name             TEXT NOT NULL DEFAULT '',
    strategy         VARCHAR(32) NOT NULL DEFAULT 'sequential',
    ring_timeout_sec INT NOT NULL DEFAULT 25 CHECK (ring_timeout_sec > 0 AND ring_timeout_sec <= 120),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT call_queues_strategy CHECK (strategy IN ('sequential', 'ring_all'))
);

CREATE TABLE IF NOT EXISTS call_queue_members (
    queue_slug        VARCHAR(64) NOT NULL REFERENCES call_queues (slug) ON DELETE CASCADE,
    extension_number  VARCHAR(32) NOT NULL REFERENCES extensions (number) ON DELETE CASCADE,
    position          INT NOT NULL DEFAULT 0,
    PRIMARY KEY (queue_slug, extension_number)
);

CREATE INDEX IF NOT EXISTS idx_queue_members_order ON call_queue_members (queue_slug, position);

-- Outbound HTTP notifications (signed with HMAC-SHA256)
CREATE TABLE IF NOT EXISTS webhooks (
    id         BIGSERIAL PRIMARY KEY,
    url        TEXT NOT NULL,
    secret     TEXT NOT NULL DEFAULT '',
    events     TEXT[] NOT NULL DEFAULT ARRAY['call.ended']::text[],
    enabled    BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE cdr ADD COLUMN IF NOT EXISTS queue_slug VARCHAR(64);
