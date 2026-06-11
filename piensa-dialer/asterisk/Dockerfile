FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Asterisk 20 (LTS) from Debian, plus gettext for envsubst.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-modules \
        gettext-base \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/asterisk/pjsip.conf /etc/asterisk/ari.conf

# Static configuration.
COPY etc/asterisk.conf   /etc/asterisk/asterisk.conf
COPY etc/modules.conf    /etc/asterisk/modules.conf
COPY etc/http.conf       /etc/asterisk/http.conf
COPY etc/rtp.conf        /etc/asterisk/rtp.conf
COPY etc/logger.conf     /etc/asterisk/logger.conf
COPY etc/extensions.conf /etc/asterisk/extensions.conf

# Templates rendered at runtime.
COPY templates/ /etc/asterisk/templates/

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 5060: SIP signalling, 8088: ARI/HTTP, 10000-10200: RTP media.
EXPOSE 5060/udp 8088/tcp 10000-10200/udp

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
