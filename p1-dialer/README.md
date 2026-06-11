# P1 · Bot de llamadas (IVR "pulse 1") por Telegram

Bot de Telegram que recibe una lista de números, **llama uno a uno** a través de
tu trunk SIP (Narayana), reproduce un aviso de voz en español
("…hemos detectado actividad inusual… si no ha sido usted, **pulse 1**…") y, si la
persona **pulsa 1**, **transfiere** la llamada a vuestro número/centralita. Te
reporta en tiempo real el resultado de cada llamada y un resumen final.

```
Telegram  ─►  Bot (Python)  ─AMI─►  Asterisk  ─SIP─►  Narayana  ─►  Teléfono
                                   │
                          IVR: aviso + "pulse 1" + transferencia
```

---

## ⚠️ Importante antes de nada

- **Esto NO corre en el agente de Cursor.** El agente es una máquina temporal sin
  acceso a la red telefónica. Este repo contiene **todo el proyecto listo para
  desplegar** en **tu VPS** (con acceso root). Las llamadas reales se prueban allí.
- **Tu trunk de Narayana tiene `Call Limit: 1`** (lo pone tu panel): solo permite
  **1 llamada simultánea**. Por eso `MAX_CONCURRENT=1` por defecto: el bot llama
  en serie. Si amplías el límite con Narayana, sube ese valor.
- **Uso legal:** las llamadas automatizadas están reguladas (LOPD/GDPR, identificación
  del llamante, consentimiento). Asegúrate de tener base legal para llamar a esas
  personas. La herramienta es neutral; el uso conforme a la ley es responsabilidad
  de quien la opera.
- **No subas secretos al repo.** La contraseña SIP, el token de Telegram, etc. van
  en el fichero `.env` (ignorado por git) o como *secrets* en el dashboard.

---

## Requisitos en la VPS

- Linux (Ubuntu/Debian recomendado) con acceso **root**.
- **Docker** y **Docker Compose** instalados.
- Tu trunk SIP de Narayana operativo.
- Puertos abiertos hacia el proveedor: SIP `5060/udp` y RTP `10000-10200/udp`.

Instalar Docker en Ubuntu (si no lo tienes):

```bash
curl -fsSL https://get.docker.com | sh
```

---

## Puesta en marcha (en tu VPS)

```bash
# 1) Clona el repo y entra en la carpeta del proyecto
git clone <tu-repo> && cd <tu-repo>/p1-dialer

# 2) Crea el fichero de configuración a partir del ejemplo
cp .env.example .env
nano .env          # rellena los valores (ver tabla abajo)

# 3) Levanta los contenedores
docker compose up -d --build

# 4) Mira los logs
docker compose logs -f
```

Para parar / reiniciar:

```bash
docker compose down         # parar
docker compose up -d --build  # reconstruir y arrancar
```

---

## Configuración (`.env`)

| Variable | Qué es |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot (de [@BotFather](https://t.me/BotFather)). |
| `ALLOWED_USER_IDS` | IDs de Telegram que pueden usar el bot, separados por comas. Si está vacío, **nadie** puede usarlo. Tu ID lo da [@userinfobot](https://t.me/userinfobot). |
| `SIP_SERVER` | `rdx.narayana.im` |
| `SIP_USERNAME` | Tu *SIP Login* (`372810444411282`). |
| `SIP_PASSWORD` | Tu *SIP Password*. **Secreto.** |
| `CALLER_ID` | Número que se muestra al llamar (`34680540787`). |
| `SIP_CODECS` | Codecs permitidos (`alaw,ulaw`). |
| `MEDIA_ENCRYPTION` | `none` (normal por UDP), `sdes` o `dtls`. Empieza con `none`. |
| `TRANSFER_NUMBER` | Número al que se transfiere al pulsar 1 (formato internacional sin `+`). |
| `DEFAULT_COUNTRY_CODE` | Prefijo para números sin `+` (`34` = España). |
| `PROMPT_TEXT` | Texto del aviso (se convierte a voz si no hay audio propio). |
| `TRANSFER_TEXT` | Aviso breve antes de transferir. |
| `MAX_CONCURRENT` | Llamadas simultáneas. **Déjalo en `1`** por el límite del trunk. |
| `DIAL_TIMEOUT` | Segundos que suena antes de darla por no contestada. |
| `DTMF_TIMEOUT` | Segundos de espera de la tecla tras el mensaje. |
| `AMI_SECRET` | Contraseña interna entre bot y Asterisk. Pon algo aleatorio. |

### Secrets en Cursor (mientras desarrollamos aquí)

En Cursor: **Cloud Agents → Secrets**. Añade ahí `SIP_PASSWORD`, `TELEGRAM_BOT_TOKEN`,
`AMI_SECRET`, etc. Se inyectan como variables de entorno y no quedan en el chat ni en el repo.

---

## La voz del aviso (TTS)

Por defecto, al arrancar se genera `prompt.wav` con **espeak-ng** (voz offline en
español, suena algo robótica). Tienes dos formas de mejorarla:

1. **Tu propia grabación** (recomendado): coloca tu audio en
   `asterisk/sounds/prompt.wav` con formato WAV PCM 16-bit, 8000 Hz, mono:

   ```bash
   sox aviso.mp3 -r 8000 -c 1 -b 16 asterisk/sounds/prompt.wav
   sox transfiriendo.mp3 -r 8000 -c 1 -b 16 asterisk/sounds/transferring.wav
   ```

   Si el fichero existe, el contenedor lo respeta y no lo regenera.

2. **Cambiar el texto**: edita `PROMPT_TEXT`/`TRANSFER_TEXT` en `.env`, borra los
   `.wav` de `asterisk/sounds/` y reinicia (`docker compose up -d --build`).

---

## Uso del bot

1. Abre el chat con tu bot en Telegram y envía `/start`.
2. Pega la lista de números (uno por línea, o separados por comas) **o** sube un
   `.txt`/`.csv`. El bot te dirá cuántos ha detectado.
3. Escribe **`/llamar`** para lanzar la campaña.
4. Recibirás un mensaje por cada llamada con su resultado y, al final, un resumen
   con quién **pulsó 1**.

**Comandos:** `/llamar`, `/estado`, `/stop`, `/ayuda`.

Formatos de número aceptados (se normalizan a internacional sin `+`):
`+34 680 54 07 87`, `0034680540787`, `680540787`, `34680540787`…

---

## Estructura del proyecto

```
p1-dialer/
├── docker-compose.yml        # une los dos servicios
├── .env.example              # plantilla de configuración
├── asterisk/                 # PBX: trunk + IVR
│   ├── Dockerfile
│   ├── entrypoint.sh         # renderiza configs y genera el TTS al arrancar
│   ├── etc/                  # plantillas pjsip / extensions / manager / ...
│   └── sounds/               # audios del IVR (prompt.wav, transferring.wav)
└── bot/                      # bot de Telegram
    ├── Dockerfile
    ├── requirements.txt
    ├── bot.py                # comandos y orquestación
    ├── ami.py                # cliente AMI: origina llamadas y traduce eventos
    ├── campaign.py           # cola, concurrencia y resultados de una campaña
    ├── phone_numbers.py      # parser/normalizador de números
    ├── config.py             # configuración desde entorno
    └── test_phone_numbers.py # tests del parser (no necesitan red)
```

---

## Resolución de problemas

- **El bot dice "No hay conexión con Asterisk":** mira `docker compose logs asterisk`.
  Comprueba que el trunk se registra: `docker compose exec asterisk asterisk -rx "pjsip show registrations"`.
- **Las llamadas no salen / no hay audio:** suele ser NAT/firewall. Asegúrate de que
  el host puede salir por `5060/udp` y el rango RTP `10000-10200/udp`, y de que
  `docker-compose.yml` usa `network_mode: host` (recomendado en Linux).
- **El proveedor rechaza el Caller ID:** algunos trunks fuerzan el caller id desde
  su panel. Si las llamadas se cuelgan al instante, prueba a ajustar `CALLER_ID`
  o consúltalo con Narayana.
- **Ver el estado del trunk:**
  ```bash
  docker compose exec asterisk asterisk -rx "pjsip show endpoints"
  docker compose exec asterisk asterisk -rx "pjsip show registrations"
  ```

---

## Estado actual

- ✅ Bot de Telegram, lógica de campaña, parser de números y reporting: **hechos y testeados**.
- ✅ Configuración de Asterisk (trunk Narayana + IVR "pulse 1" + transferencia): **lista**.
- ✅ Empaquetado con Docker Compose.
- ⏳ **Pendiente:** prueba de llamadas reales, que se hace al desplegar en una VPS
  con el trunk activo (no se puede validar desde el entorno de Cursor).
