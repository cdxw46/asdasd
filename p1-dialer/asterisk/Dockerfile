FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Asterisk + utilidades para TTS offline (espeak-ng) y conversion de audio (sox)
RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-config \
        espeak-ng \
        sox \
        libsox-fmt-all \
        gettext-base \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Plantillas de configuracion (se renderizan en el arranque con las variables)
COPY etc/ /etc/asterisk-templates/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Carpeta de sonidos personalizados
RUN mkdir -p /var/lib/asterisk/sounds/custom

EXPOSE 5060/udp 5038/tcp 10000-10200/udp

ENTRYPOINT ["/entrypoint.sh"]
