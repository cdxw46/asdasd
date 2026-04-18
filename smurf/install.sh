#!/usr/bin/env bash

set -euo pipefail

SMURF_USER="${SMURF_USER:-smurf}"
SMURF_GROUP="${SMURF_GROUP:-smurf}"
INSTALL_DIR="${INSTALL_DIR:-/opt/smurf}"
CONFIG_DIR="${CONFIG_DIR:-/etc/smurf}"
DATA_DIR="${DATA_DIR:-/var/lib/smurf}"
LOG_DIR="${LOG_DIR:-/var/log/smurf}"
VENV_DIR="${VENV_DIR:-/opt/smurf-venv}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}" && pwd)"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Este script debe ejecutarse con sudo/root."
    exit 1
  fi
}

install_packages() {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    sqlite3 \
    openssl \
    curl
}

create_user_group() {
  if ! getent group "${SMURF_GROUP}" >/dev/null 2>&1; then
    groupadd --system "${SMURF_GROUP}"
  fi
  if ! id -u "${SMURF_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${SMURF_GROUP}" --home "${DATA_DIR}" --shell /usr/sbin/nologin "${SMURF_USER}"
  fi
}

prepare_dirs() {
  mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}/tls" "${DATA_DIR}/recordings" "${DATA_DIR}/moh" "${DATA_DIR}/backups" "${LOG_DIR}"
  chown -R "${SMURF_USER}:${SMURF_GROUP}" "${DATA_DIR}" "${LOG_DIR}"
}

sync_code() {
  rm -rf "${INSTALL_DIR:?}/"*
  cp -a "${REPO_ROOT}/." "${INSTALL_DIR}/"
  chown -R root:root "${INSTALL_DIR}"
}

setup_python() {
  rm -rf "${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
  ln -sfn "${VENV_DIR}" "${INSTALL_DIR}/.venv"
}

setup_config() {
  if [[ ! -f "${CONFIG_DIR}/config.yml" ]]; then
    cp "${INSTALL_DIR}/configs/smurf.yml" "${CONFIG_DIR}/config.yml"
  fi
  sed -i "s|/opt/smurf/provisioning-templates|${INSTALL_DIR}/provisioning-templates|g" "${CONFIG_DIR}/config.yml"
  if [[ ! -f "${CONFIG_DIR}/tls/server.key" || ! -f "${CONFIG_DIR}/tls/server.crt" ]]; then
    openssl req -x509 -newkey rsa:2048 -keyout "${CONFIG_DIR}/tls/server.key" \
      -out "${CONFIG_DIR}/tls/server.crt" -days 3650 -nodes \
      -subj "/CN=smurf.local/O=SMURF PBX/C=ES"
  fi
  chown -R "${SMURF_USER}:${SMURF_GROUP}" "${CONFIG_DIR}"
  chmod 600 "${CONFIG_DIR}/tls/server.key"
  chmod 644 "${CONFIG_DIR}/tls/server.crt"
}

install_systemd_units() {
  local units=(
    smurf-pbx-core.service
    smurf-media-core.service
    smurf-sip-core.service
    smurf-api-admin.service
    smurf-provisioning.service
    smurf-watchdog.service
  )
  for unit in "${units[@]}"; do
    cp "${INSTALL_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
  done
  systemctl daemon-reload
}

enable_services() {
  local managed_by_watchdog=(
    smurf-pbx-core.service
    smurf-media-core.service
    smurf-sip-core.service
    smurf-api-admin.service
    smurf-provisioning.service
  )
  for service in "${managed_by_watchdog[@]}"; do
    systemctl disable --now "${service}" >/dev/null 2>&1 || true
    systemctl stop "${service}" || true
  done
  systemctl enable --now smurf-watchdog.service
}

main() {
  require_root
  install_packages
  create_user_group
  prepare_dirs
  sync_code
  setup_python
  setup_config
  if [[ -d "${INSTALL_DIR}/provisioning-templates" ]]; then
    chown -R "${SMURF_USER}:${SMURF_GROUP}" "${INSTALL_DIR}/provisioning-templates"
  fi
  install_systemd_units
  enable_services
  echo "SMURF instalado correctamente."
  echo "Panel admin: https://<IP_SERVIDOR>:5001"
  echo "Usuario por defecto: admin"
  echo "Password por defecto: smurfadmin"
}

main "$@"
