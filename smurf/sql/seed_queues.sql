INSERT INTO call_queues (slug, name, strategy, ring_timeout_sec)
VALUES ('support', 'Support queue', 'sequential', 20)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO call_queue_members (queue_slug, extension_number, position)
VALUES
    ('support', '1000', 10),
    ('support', '1001', 20)
ON CONFLICT DO NOTHING;
