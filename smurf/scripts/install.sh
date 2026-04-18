#!/usr/bin/env bash
# install.sh — Instalador de SMURF PBX en sistemas Debian/Ubuntu (systemd).
#
# Hace todo automáticamente:
#   * instala dependencias del sistema con apt
#   * crea usuario smurf y los directorios /opt/smurf, /etc/smurf, /var/lib/smurf, /var/log/smurf
#   * copia el código a /opt/smurf
#   * crea venv y instala dependencias Python
#   * genera certificado autofirmado para HTTPS y SIP-WSS si no existe
#   * crea config inicial y unidades systemd
#   * arranca el servicio
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "Ejecuta install.sh como root (sudo bash install.sh)"; exit 1
fi

INSTALL_DIR=${INSTALL_DIR:-/opt/smurf}
ETC_DIR=${ETC_DIR:-/etc/smurf}
VAR_DIR=${VAR_DIR:-/var/lib/smurf}
LOG_DIR=${LOG_DIR:-/var/log/smurf}
USER_NAME=${USER_NAME:-smurf}
PUBLIC_IP=${PUBLIC_IP:-}
REALM=${REALM:-smurf.local}

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> SMURF PBX installer"
echo "    código fuente : $SRC_DIR"
echo "    destino       : $INSTALL_DIR"

echo "==> Instalando dependencias del sistema (apt)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev build-essential \
    libsqlite3-dev libssl-dev libsrtp2-dev libopus-dev \
    sox ffmpeg sqlite3 openssl ca-certificates curl

echo "==> Creando usuario y directorios…"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$VAR_DIR" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$INSTALL_DIR" "$ETC_DIR/certs" "$VAR_DIR"/{recordings,voicemail,sounds,provisioning} "$LOG_DIR"
chown -R "$USER_NAME":"$USER_NAME" "$INSTALL_DIR" "$VAR_DIR" "$LOG_DIR" "$ETC_DIR"

echo "==> Copiando código a $INSTALL_DIR…"
rsync -a --delete --exclude=venv --exclude=.git --exclude='*.log' --exclude='*.db*' \
    "$SRC_DIR/" "$INSTALL_DIR/"
chown -R "$USER_NAME":"$USER_NAME" "$INSTALL_DIR"

echo "==> Creando venv…"
sudo -u "$USER_NAME" bash -lc "cd '$INSTALL_DIR' && python3 -m venv venv && \
    venv/bin/pip install --quiet --upgrade pip && \
    venv/bin/pip install --quiet \
      'fastapi==0.115.6' 'uvicorn[standard]==0.34.0' \
      'aiosqlite==0.20.0' 'pyjwt==2.10.1' \
      'python-multipart==0.0.20' 'jinja2==3.1.5' \
      'pydantic==2.10.4' 'numpy==2.2.1' 'aiofiles==24.1.0' \
      'httpx==0.28.1' 'websockets==14.1' 'cryptography==44.0.0' \
      'pyotp==2.9.0' 'qrcode==8.0' 'dnspython==2.7.0'"

echo "==> Generando certificado TLS autofirmado…"
if [[ ! -f $ETC_DIR/certs/server.crt ]]; then
    openssl req -x509 -nodes -newkey rsa:4096 -days 825 \
        -subj "/CN=$REALM/O=SMURF PBX" \
        -addext "subjectAltName=DNS:$REALM,DNS:localhost,IP:127.0.0.1$( [[ -n "$PUBLIC_IP" ]] && echo ",IP:$PUBLIC_IP" )" \
        -keyout "$ETC_DIR/certs/server.key" \
        -out    "$ETC_DIR/certs/server.crt" 2>/dev/null
    chown -R "$USER_NAME":"$USER_NAME" "$ETC_DIR/certs"
    chmod 600 "$ETC_DIR/certs/server.key"
fi

echo "==> Creando configuración inicial…"
if [[ ! -f $ETC_DIR/smurf.json ]]; then
    cp "$INSTALL_DIR/config/smurf.json.sample" "$ETC_DIR/smurf.json"
    if [[ -n "$PUBLIC_IP" ]]; then
        sed -i "s|\"public_ip\": null|\"public_ip\": \"$PUBLIC_IP\"|" "$ETC_DIR/smurf.json"
    fi
    sed -i "s|\"realm\": \"smurf.local\"|\"realm\": \"$REALM\"|" "$ETC_DIR/smurf.json"
    chown "$USER_NAME":"$USER_NAME" "$ETC_DIR/smurf.json"
fi

echo "==> Instalando systemd unit…"
install -m 644 "$INSTALL_DIR/systemd/smurfd.service" /etc/systemd/system/smurfd.service
systemctl daemon-reload
systemctl enable smurfd >/dev/null 2>&1 || true

echo "==> Arrancando smurfd…"
systemctl restart smurfd || true
sleep 2
systemctl --no-pager --lines=20 status smurfd || true

echo
echo "============================================================"
echo " SMURF PBX instalado"
echo "------------------------------------------------------------"
echo " Panel admin    : https://$( hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1):5001"
echo " API REST       : /api/v1/   (Swagger en /api/v1/docs)"
echo " Softphone web  : /softphone"
echo " SIP UDP/TCP    : :5060   ·   TLS: :5061   ·   WS: :5062   ·   WSS: :5063"
echo " RTP            : 16384-32767/udp"
echo " Provisioning   : /provisioning/<MAC>.cfg"
echo " Logs           : journalctl -u smurfd -f"
echo " Configuración  : $ETC_DIR/smurf.json"
echo " Datos          : $VAR_DIR/"
echo
echo " Usuario admin  : admin"
echo " Contraseña por defecto: smurf-admin   (cámbiala desde el panel)"
echo " Extensiones de prueba: 1000 y 1001 (passwords en logs de smurfd)"
echo "============================================================"
