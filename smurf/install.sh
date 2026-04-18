#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run install.sh as root (sudo)." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
ETC_DIR="/etc/smurf"
TLS_DIR="${ETC_DIR}/tls"
DATA_DIR="/var/lib/smurf"
BIN_DIR="/opt/smurf/bin"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="smurfd.service"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  golang-go \
  openssl \
  sqlite3

mkdir -p "${BUILD_DIR}" "${TLS_DIR}" "${DATA_DIR}" "${BIN_DIR}"

cd "${SCRIPT_DIR}"
go mod tidy
go build -o "${BUILD_DIR}/smurfd" ./cmd/smurfd

install -m 0755 "${BUILD_DIR}/smurfd" "${BIN_DIR}/smurfd"

if [[ ! -f "${TLS_DIR}/server.crt" || ! -f "${TLS_DIR}/server.key" ]]; then
  openssl req -x509 -nodes -newkey rsa:4096 \
    -keyout "${TLS_DIR}/server.key" \
    -out "${TLS_DIR}/server.crt" \
    -days 3650 \
    -subj "/CN=smurf.local"
  chmod 600 "${TLS_DIR}/server.key"
  chmod 644 "${TLS_DIR}/server.crt"
fi

if [[ ! -f "${ETC_DIR}/smurf.json" ]]; then
  cat > "${ETC_DIR}/smurf.json" <<'JSON'
{
  "domain": "smurf.local",
  "realm": "smurf.local",
  "data_dir": "/var/lib/smurf",
  "log_level": "INFO",
  "database": {
    "path": "/var/lib/smurf/smurf.db"
  },
  "sip": {
    "udp": "0.0.0.0:5060",
    "tcp": "0.0.0.0:5060",
    "tls": "0.0.0.0:5061",
    "tls_cert": "/etc/smurf/tls/server.crt",
    "tls_key": "/etc/smurf/tls/server.key",
    "nonce_ttl_seconds": 300
  },
  "rtp": {
    "bind_ip": "0.0.0.0",
    "public_ip": "127.0.0.1",
    "start_port": 20000,
    "end_port": 20998,
    "dscp": 46
  },
  "http": {
    "https": "0.0.0.0:5001",
    "tls_cert": "/etc/smurf/tls/server.crt",
    "tls_key": "/etc/smurf/tls/server.key"
  },
  "security": {
    "jwt_secret": "change-this-jwt-secret",
    "admin_username": "admin",
    "admin_password": "admin123!",
    "fail_threshold": 5,
    "block_seconds": 900,
    "admin_token_hours": 12
  }
}
JSON
  chmod 640 "${ETC_DIR}/smurf.json"
fi

install -m 0644 "${SCRIPT_DIR}/deploy/systemd/smurfd.service" "${SYSTEMD_DIR}/${SERVICE_NAME}"

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  SERVICE_STATUS="systemd service enabled and restarted"
else
  SERVICE_STATUS="systemd unit installed but not started (systemd not available in this environment)"
fi

echo
echo "SMURF installed."
echo "Admin UI: https://$(hostname -I | awk '{print $1}'):5001"
echo "Default admin: admin / admin123!"
echo "Default extension: 1000 / 12345"
echo "Service status: ${SERVICE_STATUS}"
