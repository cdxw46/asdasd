#!/usr/bin/env bash
set -euo pipefail

# SMURF single-node installer: PostgreSQL, TLS material, Go build, schema, systemd.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMURF_DB_PASSWORD="${SMURF_DB_PASSWORD:-smurf}"
SMURF_JWT_SECRET="${SMURF_JWT_SECRET:-$(openssl rand -hex 32)}"
LISTEN_IP="${SMURF_LISTEN_IP:-127.0.0.1}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y postgresql openssl ca-certificates curl

if ! command -v go >/dev/null 2>&1; then
  echo "Installing Go from upstream tarball..."
  GO_VER="1.22.2"
  curl -fsSL "https://go.dev/dl/go${GO_VER}.linux-amd64.tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go
  tar -C /usr/local -xzf /tmp/go.tgz
  export PATH="/usr/local/go/bin:$PATH"
fi

install -d -m 0755 /opt/smurf /etc/smurf

openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -subj "/CN=smurf.local" \
  -keyout /etc/smurf/tls.key -out /etc/smurf/tls.crt
chmod 0640 /etc/smurf/tls.key
chmod 0644 /etc/smurf/tls.crt

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'smurf') THEN
    CREATE ROLE smurf LOGIN PASSWORD '${SMURF_DB_PASSWORD}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE smurf OWNER smurf'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'smurf')\gexec
SQL

sudo -u postgres psql -v ON_ERROR_STOP=1 -d smurf -f "${ROOT}/sql/schema.sql"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d smurf -f "${ROOT}/sql/seed.sql"

cat > /etc/smurf/smurf.env <<ENV
SMURF_DATABASE_URL=postgres://smurf:${SMURF_DB_PASSWORD}@127.0.0.1:5432/smurf?sslmode=disable
SMURF_REALM=smurf.local
SMURF_PUBLIC_IP=${LISTEN_IP}
SMURF_RELAY_BIND=${LISTEN_IP}
SMURF_RELAY_CONTROL=127.0.0.1:19000
SMURF_SIP_UDP=0.0.0.0:5060
SMURF_SIP_TCP=0.0.0.0:5060
SMURF_SIP_TLS=
SMURF_TLS_CERT=/etc/smurf/tls.crt
SMURF_TLS_KEY=/etc/smurf/tls.key
SMURF_API_LISTEN=0.0.0.0:5001
SMURF_JWT_SECRET=${SMURF_JWT_SECRET}
ENV
chmod 0640 /etc/smurf/smurf.env

export PATH="/usr/local/go/bin:${PATH}"
cd "${ROOT}"
go mod tidy
CGO_ENABLED=0 go build -o /opt/smurf/smurfrelay ./cmd/smurfrelay
CGO_ENABLED=0 go build -o /opt/smurf/smurfsip ./cmd/smurfsip
CGO_ENABLED=0 go build -o /opt/smurf/smurfapi ./cmd/smurfapi

install -m 0644 "${ROOT}/systemd/smurfrelay.service" /etc/systemd/system/smurfrelay.service
install -m 0644 "${ROOT}/systemd/smurfsip.service" /etc/systemd/system/smurfsip.service
install -m 0644 "${ROOT}/systemd/smurfapi.service" /etc/systemd/system/smurfapi.service

systemctl daemon-reload
systemctl enable --now smurfrelay smurfsip smurfapi

echo "SMURF installed."
echo "Admin UI: https://${LISTEN_IP}:5001/ (admin / smurfadmin)"
echo "Default SIP extension 1000 / smurf1000"
echo "Edit /etc/smurf/smurf.env then: systemctl restart smurfrelay smurfsip smurfapi"
