# Despliegue en la VPS (Ubuntu 24.04) — Piensa Dialer

Guía copia-pega para dejar el bot operativo end-to-end en tu VPS.

## 0. Antes de nada

- SO: **Ubuntu 24.04**, con IP pública.
- Necesitas el `.env` (incluido en este paquete, ya con las credenciales).
- Edita en `.env` la línea de la **IP pública de tu VPS** (`SIP_EXTERNAL_IP`,
  `SIP_PUBLIC_HOST`, `PROVISION_BASE_URL`).

## 1. Instalar Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker   # opcional, para no usar sudo
```

## 2. Abrir puertos (firewall)

Si usas `ufw`:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 5060/udp
sudo ufw allow 5060/tcp
sudo ufw allow 5061/tcp
sudo ufw allow 10000:10200/udp
sudo ufw allow 8090/tcp     # QR Linphone (opcional)
sudo ufw enable
```

**Importante:** en el panel del proveedor de la VPS (Security Group / firewall
de red) abre también esos mismos puertos, no solo en `ufw`.

## 3. Copiar el proyecto y configurar

```bash
cd ~
# (este paquete ya trae la carpeta piensa-dialer con el .env dentro)
cd piensa-dialer
nano .env        # pon la IP pública de tu VPS donde indica
```

Valores de `.env` que SÍ o SÍ debes revisar:

- `SIP_EXTERNAL_IP=` → IP pública de la VPS (para que el audio RTP funcione).
- `SIP_PUBLIC_HOST=` → la misma IP pública (va en los datos del agente).
- `PROVISION_BASE_URL=http://TU_IP:8090` → la misma IP pública.

El resto (token de Telegram, SIP de Narayana, contraseñas) ya está puesto.

## 4. Levantar

```bash
docker compose up -d --build
docker compose logs -f          # mira que arranca bien (Ctrl+C para salir)
```

## 5. Comprobar que el trunk registra

```bash
docker compose exec asterisk asterisk -rx "pjsip show registrations"
# Debe salir: narayana-reg ... Registered
```

## 6. Probar

1. En Telegram, `/start` → menú.
2. **👥 Agentes → ➕ Crear agente** → te da datos + QR.
3. Registra **PortSIP** con esos datos (SIP server = IP de la VPS, usuario,
   password, outbound proxy `IP:5060`, transporte UDP). Debe quedar registrado.
4. **📞 Llamar** → pega un número → contesta, escucha la locución, pulsa **1**
   → te entra en PortSIP.

Comprobar el agente registrado:

```bash
docker compose exec asterisk asterisk -rx "pjsip show contacts"
```

## 7. Seguir con Cursor en la VPS

- Instala Cursor / el agente como ya hiciste.
- Abre la carpeta `piensa-dialer`.
- Lee `CONTEXT.md` (resumen completo de qué es, qué funciona y qué falta) para
  retomar la conversación exactamente donde la dejamos.

## Notas

- Recargar tras tocar `.env`: `docker compose up -d` (no hace falta `--build`
  salvo que cambie el código).
- Logs del bot: `docker compose logs -f bot`.
- Logs de Asterisk: `docker compose logs -f asterisk`.
- Cambiar locución / agentes: todo desde el bot de Telegram.
