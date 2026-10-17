#!/usr/bin/env bash
# Резервная копия тома данных в ./backups/maverick-data-ДАТА.tgz (база пользователей снимается консистентно средствами SQLite).
#   deploy/backup.sh          — полная копия
#   deploy/backup.sh --small  — без кеша свечей binanceusdm_*.csv (его можно скачать заново)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
docker compose exec -T backend python - <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/maverick.db")
dst = sqlite3.connect("/app/data/maverick.db.bak")
src.backup(dst)
dst.close()
src.close()
PY
exclude=(--exclude='maverick.db' --exclude='maverick.db-wal' --exclude='maverick.db-shm')
[ "${1:-}" = "--small" ] && exclude+=(--exclude='binanceusdm_*' --exclude='binance_*')
out="maverick-data-$(date +%F-%H%M).tgz"
docker run --rm -u root -v maverick_data:/data:ro -v "$PWD/backups":/out --entrypoint tar maverick-app:latest czf "/out/$out" -C /data "${exclude[@]}" .
docker compose exec -T backend rm -f /app/data/maverick.db.bak
echo "Готово: backups/$out (в архиве база лежит как maverick.db.bak)"
