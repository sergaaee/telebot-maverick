#!/bin/sh
# Раз в 6 часов перечитывает конфигурацию nginx, чтобы подхватить продлённый сертификат (certbot обновляет файлы в томе).
( while :; do sleep 6h; nginx -s reload >/dev/null 2>&1 || true; done ) &
