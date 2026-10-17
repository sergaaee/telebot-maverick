#!/bin/sh
# Выбирает конфиг nginx при старте контейнера: HTTPS, если для DOMAIN уже выпущен сертификат Let's Encrypt, иначе HTTP.
# Официальный entrypoint потом подставляет переменные окружения (envsubst) в /etc/nginx/templates/*.template.
set -e
domain="${DOMAIN:-_}"
cert="/etc/letsencrypt/live/${domain}/fullchain.pem"
mkdir -p /etc/nginx/templates /var/www/certbot
if [ "$domain" != "_" ] && [ -f "$cert" ] && [ "${FORCE_HTTP:-0}" != "1" ]; then
    mode=https
    echo 'add_header Strict-Transport-Security "max-age=31536000" always;' > /etc/nginx/snippets/hsts.conf
else
    mode=http
    : > /etc/nginx/snippets/hsts.conf
fi
cp "/etc/nginx/templates-src/${mode}.conf.template" /etc/nginx/templates/default.conf.template
echo "10-select-mode: DOMAIN=${domain}, режим ${mode}"
