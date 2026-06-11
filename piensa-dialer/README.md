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

- Lista de números pegada en Telegram (uno por línea, comas o espacios).
- Confirmación con botón antes de llamar.
- Llamadas en paralelo con límite configurable (`MAX_CONCURRENT_CALLS`).
- Mensaje de voz por TTS en español (se genera solo) o texto propio.
- Captura del DTMF: al pulsar **1** se hace puente con el agente.
- Mensaje de estado en vivo + informe final (contestó / pulsó 1 / no contesta /
  comunica / fallida…).
- Lista blanca opcional de usuarios de Telegram.

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
        ├── main.py           # interfaz Telegram
        ├── dialer.py         # motor de campaña (estados de llamada)
        ├── ari.py            # cliente ARI async (REST + websocket)
        ├── tts.py            # generación del mensaje de voz
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

## Red / firewall

Asterisk corre en modo `network_mode: host` para que SIP y RTP usen la IP real
del servidor. Abre en el firewall de la VPS:

- **UDP 5060** — señalización SIP.
- **UDP 10000–10200** — audio RTP (rango configurable en `asterisk/etc/rtp.conf`).
- **TCP 8088** (ARI) **no** debe exponerse a internet; solo lo usa el bot en
  `127.0.0.1`.

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
| `/start`  | Ayuda y tu Telegram ID.                 |
| `/config` | Configuración activa.                   |
| `/status` | Progreso de la campaña actual.          |
| `/stop`   | Cancela la campaña en curso.            |
| *(texto)* | Pegar números → confirmar → llamar.     |

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
