-- Allow WebSocket SIP registrations (run once on existing DBs created before WS support).
ALTER TABLE registrations DROP CONSTRAINT IF EXISTS registrations_transport_check;
ALTER TABLE registrations ADD CONSTRAINT registrations_transport_check
  CHECK (transport IN ('udp', 'tcp', 'tls', 'ws'));
