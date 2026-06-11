# Piensa Dialer

Bot de Telegram que lanza **llamadas salientes automáticas** a través de tu
trunk SIP (Narayana) usando Asterisk. Le pegas una lista de números, el bot
llama uno a uno, reproduce un mensaje de voz en español (*"hemos detectado
actividad inusual… si no ha sido usted pulse 1"*), detecta la tecla que pulsan
(DTMF) y, si pulsan **1**, transfiere la llamada a tu agente / centralita.

```
Telegram  ──►  Bot (Python)  ──ARI──►  Asterisk  ──SIP──►  Narayana  ──►  teléfonos
                    ▲                                                        │
                    └──────────────  informe de resultados  ◄───────────────┘
```

## Qué hace

- Menú con botones (Llamar · Locuciones · Agentes · Historial · Configuración).
- **Gestión de agentes desde el bot**: crear/borrar usuarios SIP y generar un
  **QR** para configurar Zoiper/PortSIP en segundos.
- Lista de números pegada en Telegram (uno por línea, comas o espacios).
- Confirmación con botón antes de llamar.
- Llamadas en paralelo con límite configurable (`MAX_CONCURRENT_CALLS`).
- **Locuciones**: sube tus propios audios (MP3/OGG/M4A/nota de voz) o genera
  la locución desde texto (TTS español). Eliges cuál suena al cliente.
- **Locución de agente**: al transferir, antes de unir la llamada se reproduce
  un audio de identificación al agente ("te paso una verificación…").
- Captura del DTMF: al pulsar **1** se hace puente con el agente.
- **Agente = softphone SIP** (Zoiper/PortSIP): no se transfiere a un número de
  teléfono, sino a una extensión SIP que el agente registra en su softphone
  (sin coste de minutos de transferencia). También se soporta transferir a un
  número PSTN si se prefiere (`AGENT_MODE=number`).
- Mensaje de estado en vivo + informe final (contestó / pulsó 1 / no contesta /
  comunica / fallida…) e historial de campañas.
- Lista blanca opcional de usuarios de Telegram.

### Locuciones (cómo subir un MP3)

1. En el bot: menú → **🎙 Locuciones** → **➕ Subir MP3**.
2. Envía el archivo de audio (puedes poner el nombre en el *caption*).
3. Elige el rol: **Cliente** (lo que oye quien recibe la llamada) o **Agente**
   (lo que oye tu equipo al recibir la transferencia).
4. Se convierte a 8 kHz mono y queda activa. Puedes tener varias y cambiar la
   activa con un toque. También **✍️ Desde texto** crea una locución por TTS.

## Estructura

```
piensa-dialer/
├── docker-compose.yml        # levanta Asterisk + bot
├── .env.example              # configuración (cópialo a .env)
├── asterisk/                 # Asterisk (PJSIP + ARI + Stasis)
│   ├── Dockerfile
│   ├── docker-entrypoint.sh
│   ├── etc/                  # config estática
│   └── templates/            # pjsip.conf / ari.conf (con secretos en runtime)
└── bot/                      # bot de Telegram
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py           # interfaz Telegram (menú, locuciones, campañas)
        ├── dialer.py         # motor de campaña (estados de llamada)
        ├── ari.py            # cliente ARI async (REST + websocket)
        ├── locuciones.py     # librería de locuciones (MP3/TTS + índice)
        ├── agentes.py        # usuarios SIP dinámicos + include PJSIP
        ├── ami.py            # cliente AMI (recarga PJSIP)
        ├── provisioning.py   # servidor HTTP de provisioning (QR Zoiper)
        ├── qr.py             # generación de QR
        ├── tts.py            # conversión de audio + síntesis de voz
        ├── numbers.py        # parseo/normalización de números
        └── config.py
```

## Puesta en marcha (en tu VPS)

> Requisitos: una VPS con IP pública, Docker y Docker Compose. La sandbox de
> Cursor **no** sirve para llamar de verdad (el tráfico SIP/RTP está bloqueado y
> es efímera): esto se despliega en tu servidor.

1. Instala Docker (Ubuntu/Debian):

```bash
curl -fsSL https://get.docker.com | sh
```

2. Clona el repo y entra en la carpeta:

```bash
cd piensa-dialer
cp .env.example .env
nano .env          # rellena los secretos (ver abajo)
```

3. Variables mínimas a rellenar en `.env`:

   - `TELEGRAM_BOT_TOKEN` — token de @BotFather.
   - `SIP_PASSWORD` — contraseña SIP del panel de Narayana.
   - `AGENT_NUMBER` — número al que se transfiere al pulsar 1 (E.164 sin `+`).
   - `ARI_PASSWORD` — pon una contraseña fuerte (es interna).
   - (Ya vienen puestos `SIP_SERVER=rdx.narayana.im`, `SIP_LOGIN` y
     `CALLER_ID=34680540787`; cámbialos si hace falta.)
   - Si tu VPS está detrás de NAT, pon `SIP_EXTERNAL_IP` con su IP pública.

4. Levanta todo:

```bash
docker compose up -d --build
docker compose logs -f
```

5. En Asterisk comprueba que el trunk registra:

```bash
docker compose exec asterisk asterisk -rx "pjsip show registrations"
docker compose exec asterisk asterisk -rx "pjsip show endpoints"
```

   Debe aparecer `narayana-reg` en estado **Registered**.

6. Abre Telegram, habla con tu bot, `/start`, pega una lista de números y pulsa
   **📞 Llamar**.

## Agentes (Zoiper / PortSIP) y QR

Por defecto (`AGENT_MODE=sip`) al pulsar **1** la llamada va a los **softphones
SIP** de los agentes (grupo de timbrado: suena en todos los registrados y el
primero que descuelga se queda la llamada).

Los agentes se gestionan **desde el bot** (menú → **👥 Agentes**):

- **➕ Crear agente**: escribes un nombre y el bot crea un usuario SIP con
  contraseña aleatoria, recarga Asterisk (vía AMI) y te devuelve un **QR**.
- El agente abre Zoiper → «Iniciar sesión con QR» → escanea → queda
  configurado solo (provisioning XML servido por el bot).
- **🗑** borra el agente (y recarga Asterisk).

Para que el QR funcione, el móvil del agente debe poder alcanzar la URL de
provisioning (`PROVISION_BASE_URL`, p. ej. `http://TU_IP:8090`); abre ese
puerto en el firewall. Las credenciales también se muestran en texto por si se
prefiere configurarlas a mano.

Comprueba que un agente está registrado:

```bash
docker compose exec asterisk asterisk -rx "pjsip show contacts"
```

Si prefieres transferir a un número de teléfono en vez de a softphones, pon
`AGENT_MODE=number` y `AGENT_NUMBER=...`.

## Red / firewall

Asterisk corre en modo `network_mode: host` para que SIP y RTP usen la IP real
del servidor. Abre en el firewall de la VPS:

- **UDP 5060** (y TCP 5060 / TLS 5061) — señalización SIP: trunk **y** registro
  de los softphones de los agentes (Zoiper/PortSIP).
- **UDP 10000–10200** — audio RTP (rango configurable en `asterisk/etc/rtp.conf`).
- **TCP 8090** (`PROVISION_PORT`) — servidor de provisioning para el QR de
  Zoiper (los móviles de los agentes lo consultan). Exponerlo solo si usas QR.
- **TCP 8088** (ARI) y **TCP 5038** (AMI) **no** deben exponerse a internet;
  solo los usa el bot en `127.0.0.1`.

## Cambiar el mensaje

Por defecto se usa un mensaje en español incorporado. Para cambiarlo, edita
`MESSAGE_TEXT` en `.env` y reinicia el bot:

```bash
docker compose restart bot
```

El audio se regenera automáticamente (gTTS → WAV 8 kHz mono) y queda en el
volumen compartido con Asterisk. Si prefieres un audio pregrabado tuyo, coloca
un `piensa-aviso.wav` (PCM 16-bit, 8000 Hz, mono) en el volumen `sounds` y
desactiva la regeneración borrando `MESSAGE_TEXT`.

## Comandos del bot

| Comando   | Acción                                  |
|-----------|-----------------------------------------|
| `/start` `/menu` | Menú principal con botones.      |
| `/status` | Progreso de la campaña actual.          |
| `/stop`   | Cancela la campaña en curso.            |
| *(texto)* | Pegar números → confirmar → llamar.     |
| *(audio)* | Subir una locución (MP3/voz).           |

## Desarrollo / tests

```bash
cd bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest
```

## Aviso legal

Las llamadas automáticas están reguladas (LOPD/GDPR, ePrivacy). Asegúrate de
tener base legal y consentimiento para llamar, identifica correctamente al
llamante y respeta horarios y listas de exclusión. Esta herramienta es el medio;
el uso conforme a la normativa es responsabilidad de quien la opera.
```
