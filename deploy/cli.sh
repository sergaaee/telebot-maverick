#!/usr/bin/env bash
# Консольные команды приложения внутри контейнера backend:
#   deploy/cli.sh user add ЛОГИН          — создать пользователя сайта и бота (пароль спросит)
#   deploy/cli.sh user list
#   deploy/cli.sh export data/history.csv --chat 3217240841     — первая выгрузка истории (спросит код из Telegram)
#   deploy/cli.sh backtest --help
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose exec backend python /app/backend/cli.py "$@"
