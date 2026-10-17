#!/usr/bin/env bash
# Выпуск сертификата Let's Encrypt и переключение сайта на HTTPS.
#   deploy/get-cert.sh your.domain you@mail.com [--staging]
# Требования: A-запись домена указывает на этот сервер, порты 80 и 443 открыты.
set -euo pipefail
cd "$(dirname "$0")/.."
DOMAIN="${1:?Укажите домен: deploy/get-cert.sh your.domain you@mail.com}"
EMAIL="${2:?Укажите e-mail для уведомлений Let’s Encrypt}"
STAGING=""; [ "${3:-}" = "--staging" ] && STAGING="--staging"
[ -f .env ] || { echo "Нет .env — скопируйте .env.server.example"; exit 1; }

setvar() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
setvar DOMAIN "$DOMAIN"
setvar FORCE_HTTP 0

# 1. сайт в режиме HTTP отдаёт /.well-known/acme-challenge для проверки домена
docker compose up -d --build backend web
sleep 3
# 2. выпуск сертификата
docker compose --profile tls run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
    -d "$DOMAIN" --email "$EMAIL" --agree-tos --no-eff-email $STAGING
# 3. nginx при старте видит сертификат и включает HTTPS; cookie сессии становится Secure
setvar COOKIE_SECURE true
docker compose up -d --force-recreate backend web
# 4. автопродление (certbot проверяет раз в 12 часов, nginx перечитывает сертификаты раз в 6 часов)
docker compose --profile tls up -d certbot
echo "Готово: https://$DOMAIN"
