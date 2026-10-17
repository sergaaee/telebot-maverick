#!/usr/bin/env bash
# Обновление сервера: забрать свежий код, пересобрать образы и перезапустить сервисы (данные в томах сохраняются).
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose build --pull
docker compose up -d --remove-orphans
docker image prune -f >/dev/null
docker compose ps
