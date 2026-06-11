#!/usr/bin/env bash
set -euo pipefail

TPL=/etc/asterisk-templates
DST=/etc/asterisk

echo "[entrypoint] Renderizando configuracion de Asterisk..."

# Variables con valores por defecto seguros
export SIP_SERVER="${SIP_SERVER:-rdx.narayana.im}"
export SIP_USERNAME="${SIP_USERNAME:?Falta SIP_USERNAME}"
export SIP_PASSWORD="${SIP_PASSWORD:?Falta SIP_PASSWORD}"
export CALLER_ID="${CALLER_ID:-}"
export SIP_CODECS="${SIP_CODECS:-alaw,ulaw}"
export MEDIA_ENCRYPTION="${MEDIA_ENCRYPTION:-none}"
export TRANSFER_NUMBER="${TRANSFER_NUMBER:?Falta TRANSFER_NUMBER}"
export DTMF_TIMEOUT="${DTMF_TIMEOUT:-8}"
export DIAL_TIMEOUT="${DIAL_TIMEOUT:-30}"
export AMI_USER="${AMI_USER:-p1bot}"
export AMI_SECRET="${AMI_SECRET:?Falta AMI_SECRET}"
export PROMPT_TEXT="${PROMPT_TEXT:-Hola. Si no ha sido usted, pulse uno.}"
export TRANSFER_TEXT="${TRANSFER_TEXT:-Un momento, le paso con un agente.}"

# Traducir MEDIA_ENCRYPTION a la directiva de PJSIP
case "$MEDIA_ENCRYPTION" in
  sdes) export PJSIP_MEDIA_ENC="media_encryption=sdes" ;;
  dtls) export PJSIP_MEDIA_ENC="media_encryption=dtls" ;;
  *)    export PJSIP_MEDIA_ENC="; media_encryption=none" ;;
esac

# Convertir codecs "alaw,ulaw" en lineas allow=
ALLOW_LINES=""
IFS=',' read -ra CODECS <<< "$SIP_CODECS"
for c in "${CODECS[@]}"; do
  c_trim="$(echo "$c" | xargs)"
  [ -n "$c_trim" ] && ALLOW_LINES+="allow=${c_trim}"$'\n'
done
export ALLOW_LINES

# IMPORTANTE: pasamos a envsubst SOLO la lista de variables a sustituir.
# Asi NO toca las variables propias del dialplan de Asterisk como
# ${CAMPAIGN}, ${TARGET}, ${DIALSTATUS} o ${EXTEN}, que deben quedar intactas.
render() {
  local name="$1"
  local vars="$2"
  envsubst "$vars" < "$TPL/$name.template" > "$DST/$name"
  echo "[entrypoint]   -> $DST/$name"
}

render pjsip.conf      '${SIP_SERVER} ${SIP_USERNAME} ${SIP_PASSWORD} ${ALLOW_LINES} ${PJSIP_MEDIA_ENC}'
render extensions.conf '${TRANSFER_NUMBER} ${DTMF_TIMEOUT} ${DIAL_TIMEOUT} ${CALLER_ID}'
render manager.conf    '${AMI_USER} ${AMI_SECRET}'

# Ficheros estaticos
cp -f "$TPL/asterisk.conf" "$DST/asterisk.conf"
cp -f "$TPL/modules.conf"  "$DST/modules.conf"
cp -f "$TPL/logger.conf"   "$DST/logger.conf"
cp -f "$TPL/rtp.conf"      "$DST/rtp.conf"

# --- Generar el audio del aviso si no hay uno propio ---
SOUNDS=/var/lib/asterisk/sounds/custom
if [ ! -f "$SOUNDS/prompt.wav" ]; then
  echo "[entrypoint] Generando prompt.wav con espeak-ng (TTS offline en espanol)..."
  espeak-ng -v es -s 150 -w /tmp/prompt_raw.wav "$PROMPT_TEXT" || true
  if [ -f /tmp/prompt_raw.wav ]; then
    sox /tmp/prompt_raw.wav -r 8000 -c 1 -b 16 "$SOUNDS/prompt.wav"
  fi
else
  echo "[entrypoint] Usando prompt.wav existente (audio propio)."
fi

if [ ! -f "$SOUNDS/transferring.wav" ]; then
  echo "[entrypoint] Generando transferring.wav..."
  espeak-ng -v es -s 150 -w /tmp/transfer_raw.wav "$TRANSFER_TEXT" || true
  if [ -f /tmp/transfer_raw.wav ]; then
    sox /tmp/transfer_raw.wav -r 8000 -c 1 -b 16 "$SOUNDS/transferring.wav"
  fi
fi

# Permisos
chown -R asterisk:asterisk /var/lib/asterisk/sounds /var/log/asterisk 2>/dev/null || true

echo "[entrypoint] Arrancando Asterisk en primer plano..."
exec asterisk -f -vvvg
