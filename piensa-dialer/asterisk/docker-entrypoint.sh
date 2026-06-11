#!/usr/bin/env bash
set -euo pipefail

# Render the config templates that need secrets / runtime values.
# Only the listed variables are substituted so we don't accidentally
# eat Asterisk's own ${...} dialplan variables.
render() {
    local src="$1" dst="$2"
    envsubst '${SIP_SERVER} ${SIP_LOGIN} ${SIP_PASSWORD} ${CALLER_ID} ${SIP_EXTERNAL_IP} ${ARI_USERNAME} ${ARI_PASSWORD}' \
        < "$src" > "$dst"
    echo "rendered $dst"
}

: "${SIP_SERVER:?SIP_SERVER is required}"
: "${SIP_LOGIN:?SIP_LOGIN is required}"
: "${SIP_PASSWORD:?SIP_PASSWORD is required}"
: "${CALLER_ID:?CALLER_ID is required}"
: "${ARI_USERNAME:?ARI_USERNAME is required}"
: "${ARI_PASSWORD:?ARI_PASSWORD is required}"
export SIP_EXTERNAL_IP="${SIP_EXTERNAL_IP:-}"

render /etc/asterisk/templates/pjsip.conf.template /etc/asterisk/pjsip.conf
render /etc/asterisk/templates/ari.conf.template   /etc/asterisk/ari.conf

# Make sure runtime dirs exist. Custom sounds live under the data dir so that
# `sound:custom/<name>` resolves (Debian Asterisk data dir = /usr/share/asterisk).
mkdir -p /var/run/asterisk /usr/share/asterisk/sounds/en/custom
chown -R root:root /var/run/asterisk || true

echo "Starting Asterisk..."
exec asterisk -f -vvv
