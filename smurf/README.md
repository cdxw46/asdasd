# SMURF PBX

**SMURF** es una plataforma PBX empresarial completa, escrita íntegramente desde cero, sin usar Asterisk, FreeSWITCH, Kamailio, OpenSIPS ni ningún otro PBX o stack SIP como base. Implementa SIP, RTP, SDP, autenticación digest, B2BUA, dial plan, colas, IVR, voicemail, conferencias, WebRTC, fax (estructura), provisioning de teléfonos IP, panel de administración SPA y API REST + WebSocket para automatización.

## Tabla de contenidos

- [Características](#características)
- [Requisitos](#requisitos)
- [Instalación rápida](#instalación-rápida)
- [Configuración](#configuración)
- [Puertos utilizados](#puertos-utilizados)
- [Credenciales por defecto](#credenciales-por-defecto)
- [Arquitectura](#arquitectura)
- [API y desarrollo](#api-y-desarrollo)
- [Tests](#tests)

## Características

- **Stack SIP propio** (RFC 3261, 3262, 3265, 3515, 3856, 7118)
  - Transports UDP / TCP / TLS / WS / WSS
  - Autenticación digest MD5, SHA-256 y SHA-256-sess (RFC 7616/8760)
  - Dialog/transaction layer completo, ACK, CANCEL, BYE, REFER, UPDATE, INFO, MESSAGE
  - Registrar con NAT traversal (`rport`, `received`), Path (RFC 3327), Min-Expires
- **Motor RTP/RTCP propio** (RFC 3550, 3551, 4733, 3711)
  - Codecs G.711 µ-law / A-law, G.722 (relay), Opus (relay/WebRTC), telephone-event
  - Jitter buffer adaptativo, transcoding PCM 16 bit, mezcla N-1 para conferencias
  - DSCP marking, RTCP Sender Reports periódicos
  - DTMF RFC 4733 y SIP INFO
  - Síntesis interna de tonos (dial, ringback, busy, MoH)
  - Grabación de llamadas a WAV estéreo
- **B2BUA / lógica PBX**
  - Dial plan con regex y prioridades
  - Extensiones SIP, ring groups (ringall/hunt/random), colas (roundrobin/leastrecent/random/priority), IVR multinivel
  - Voicemail por extensión con grabación, listado y descarga
  - Conferencias multipartícipe con mezcla
  - Trunks salientes con autenticación digest, failover entre trunks
  - Reglas inbound (DIDs) y outbound con strip/prepend
  - Transferencias ciegas (REFER) y atendidas, hold/unhold (re-INVITE)
  - Chat IM SIP (RFC 3428) con almacenamiento y forwarding
- **Softphone WebRTC** integrado en la SPA, sin librerías SIP de terceros
  - SIP-over-WebSocket (RFC 7118) implementado a mano
  - Digest auth en navegador con MD5 puro JS
  - getUserMedia + RTCPeerConnection para audio
  - DTMF outband, llamadas entrantes y salientes, dialpad completo
- **Panel de administración SPA**
  - Diseño dark moderno, responsive, sin frameworks externos
  - Dashboard con KPIs y stream de eventos en vivo (WebSocket)
  - CRUD de extensiones, trunks, dial plan, colas/IVRs/ring-groups
  - CDR con filtros y descarga CSV
  - Reproducción y descarga de grabaciones
  - Buzón de voz: listado y reproducción inline
  - Backup/restore en JSON
- **API REST completa** + OpenAPI/Swagger autogenerada (`/api/v1/docs`)
  - Autenticación JWT (cookie httpOnly o `Authorization: Bearer`)
  - 2FA TOTP opcional (Google Authenticator, etc.)
  - WebSocket de eventos `/api/v1/ws/events`
- **Servidor de provisioning**
  - HTTP/HTTPS: `/provisioning/<MAC>.cfg`
  - Plantillas para Yealink, Snom, Fanvil, Grandstream, Polycom, Cisco
- **Seguridad**
  - Fail2ban interno + rate limiting por IP
  - Bloqueo opcional automático con `iptables`
  - HMAC-firmado nonces digest
  - Roles: superadmin / admin / supervisor / user
- **Persistencia**
  - SQLite (modo WAL) para configuración, CDR, voicemail, chat, provisioning
  - Backup/restore en un click

## Requisitos

- Linux (Debian / Ubuntu 22.04+ probado, otras distros con `systemd` y Python 3.11+)
- Acceso `sudo`
- Conexión a Internet para `apt install` y `pip install`

## Instalación rápida

```bash
# Como root o con sudo
sudo bash scripts/install.sh
# Personalizable:
sudo PUBLIC_IP=203.0.113.10 REALM=pbx.example.com bash scripts/install.sh
```

El instalador:

1. Instala dependencias del sistema con `apt`.
2. Crea el usuario `smurf`.
3. Copia el código a `/opt/smurf` y crea su `venv`.
4. Genera certificado TLS autofirmado en `/etc/smurf/certs/`.
5. Crea `/etc/smurf/smurf.json` con la configuración.
6. Instala y arranca la unidad `systemd` `smurfd`.

Tras la instalación abre `https://<IP-de-tu-servidor>:5001` en tu navegador.

## Modo desarrollo

```bash
git clone <este-repo> smurf && cd smurf
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python3 -m smurfd.server   # arranca todo en primer plano
```

Por defecto usa `./config/smurf.json` si existe, si no aplica defaults razonables (escucha en todas las interfaces).

## Configuración

Toda la configuración estática vive en `/etc/smurf/smurf.json` (o `./config/smurf.json` en desarrollo). Cambios en extensiones, trunks, dial plan, colas, etc. se gestionan vía la SPA o la API y se persisten en SQLite.

Reinicia el servicio para aplicar cambios en la configuración estática:

```bash
sudo systemctl restart smurfd
```

## Puertos utilizados

| Puerto    | Protocolo | Servicio                                 |
|-----------|-----------|------------------------------------------|
| 5060/udp  | SIP       | Registrar / B2BUA UDP                    |
| 5060/tcp  | SIP       | Registrar / B2BUA TCP                    |
| 5061/tcp  | SIP/TLS   | SIP cifrado                              |
| 5062/tcp  | SIP/WS    | SIP-over-WebSocket (no cifrado, dev)     |
| 5063/tcp  | SIP/WSS   | SIP-over-WebSocket cifrado (WebRTC)      |
| 5000/tcp  | HTTP      | Panel admin (sin TLS)                    |
| 5001/tcp  | HTTPS     | Panel admin (TLS)                        |
| 16384-32767/udp | RTP/RTCP | Medios                              |

## Credenciales por defecto

- **Usuario admin**: `admin`  ·  **password**: `smurf-admin`
- Aparece en el log al primer arranque (`journalctl -u smurfd`).
- Cambiar inmediatamente desde *Ajustes → Cambiar contraseña*.

Las extensiones de prueba `1000` y `1001` se crean automáticamente y sus contraseñas SIP se imprimen también al primer arranque.

## Arquitectura

```
┌──────────────────────────────────────────────────────────────────┐
│                          Panel SPA + API                          │
│  HTML/JS/CSS vanilla + WebRTC nativo · FastAPI/Starlette host    │
└─────────────────────────▲────────────────────────────────────────┘
                          │ REST + WS  (JWT)
┌─────────────────────────┴────────────────────────────────────────┐
│                       SmurfServer (orquestador)                  │
│  ┌──────────────┬──────────────┬───────────────┬──────────────┐  │
│  │  Registrar   │   B2BUA      │  Dial plan    │  Eventos bus │  │
│  └──────┬───────┴──────┬───────┴──────┬────────┴──────┬───────┘  │
│  ┌──────┴──┐  ┌────────┴──────┐ ┌─────┴──────┐  ┌────┴──────┐    │
│  │ TX layer│  │ Dialog layer  │ │  RTP relay │  │ Conf bridge│   │
│  └────┬────┘  └───────────────┘ └─────┬──────┘  └────┬──────┘    │
│  ┌────┴───────────────────────────────┴───────────────┴────┐     │
│  │           SIP Transports (UDP/TCP/TLS/WS/WSS)            │     │
│  └──────────────────────────────────────────────────────────┘     │
│                          ▲                                       │
│                          │                                       │
│  ┌─────────────────────┐ │  ┌──────────────────────────────┐     │
│  │   SQLite (WAL)      │◄┘  │   Filesystem (recordings,    │     │
│  │  configuración/CDR/ │    │   voicemail, sounds, certs)  │     │
│  │  voicemail/chat     │    └──────────────────────────────┘     │
│  └─────────────────────┘                                          │
└──────────────────────────────────────────────────────────────────┘
```

Cada componente se aloja en un módulo independiente bajo `smurfd/`:

```
smurfd/
├── sip/           Stack SIP (uri, message, transport, transaction, dialog, registrar, sdp, auth)
├── rtp/           Engine, codecs, jitter, conference, recorder, sounds, wavfile, packet
├── pbx/           B2BUA, dial plan, queue manager, IVR runner, eventos, trunk auth
├── db/            Schema y wrapper async SQLite
├── api/           API REST + JWT + SPA serving
├── provisioning/  Plantillas para teléfonos IP
├── security/      Fail2ban interno + rate limit
├── util/          Config, logging, password hashing
└── server.py      Orquestador entry-point (`python -m smurfd.server`)
```

## API y desarrollo

- Documentación interactiva: `https://<host>:5001/api/v1/docs`
- Esquema OpenAPI: `/api/v1/openapi.json`
- Autenticación: `POST /api/v1/auth/login` → recibe `token` (JWT). Mándalo en `Authorization: Bearer <token>` o vía cookie `smurf_token`.
- Eventos en tiempo real: `wss://<host>:5001/api/v1/ws/events?token=<JWT>` emite todos los eventos del bus (call.start, call.answered, call.end, voicemail.new, registrar.update, chat.message…).

Ejemplos:

```bash
TOKEN=$(curl -s -X POST https://localhost:5001/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"smurf-admin"}' --insecure | jq -r .token)

curl -s -H "Authorization: Bearer $TOKEN" https://localhost:5001/api/v1/dashboard --insecure | jq
curl -s -H "Authorization: Bearer $TOKEN" https://localhost:5001/api/v1/extensions --insecure | jq
```

## Tests

Smoke test funcional incluido (REGISTER + INVITE + ACK + BYE entre dos UAs):

```bash
. venv/bin/activate
python -m unittest tests.test_register_invite -v
```

## Notas operativas

- Para producción configura `public_ip` con la IP pública del PBX (NAT) y abre los puertos en el firewall.
- El softphone WebRTC requiere HTTPS (los navegadores no permiten `getUserMedia` por HTTP en hosts no locales).
- Los certificados autofirmados generados por `install.sh` provocan advertencias en el navegador; añade un certificado válido (Let's Encrypt) en `/etc/smurf/certs/` y reinicia.
- `iptables` se usa opcionalmente para el bloqueo de IPs maliciosas; si no está disponible, los bloqueos quedan en RAM.

## Licencia

Código propio de SMURF. Uso interno y empresarial.
