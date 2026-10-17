# Развёртывание на VPS (Docker + nginx)

Всё собирается и запускается из контейнеров: `docker compose up -d --build`.

```
интернет ──► web (nginx :80/:443, фронтенд, TLS, ограничение частоты запросов)
                  └─► backend (FastAPI :8000, только во внутренней сети)   ┐
             signalbot (aiogram, Telegram-бот сигналов)                    ├─ общий том data
                                                                           ┘  (база, история, свечи, сессия Telegram)
             certbot (Let's Encrypt, по профилю tls)
```

Наружу публикуются только порты 80 и 443. backend и бот снаружи недоступны. Backend запускается в **одном** процессе (фоновые подгрузки истории и свечей живут в нём).

## 1. Сервер

Подойдёт любой VPS с Ubuntu 22.04+ / Debian 12 (1 vCPU, 1–2 ГБ RAM, 10+ ГБ диска; свечи BTC за полгода занимают порядка десятков МБ).

```bash
curl -fsSL https://get.docker.com | sh                 # Docker + плагин compose
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
git clone -b server <адрес-репозитория> maverick && cd maverick
```

Для HTTPS домену нужна A-запись на IP сервера. Без домена сайт работает по `http://IP` (см. шаг 5).

## 2. Настройка

```bash
cp .env.server.example .env
nano .env
```

Минимум: `TG_API_ID` и `TG_API_HASH` (my.telegram.org → API development tools), `SIGNALBOT_TOKEN` (если нужен бот), `SYMBOLS`. Остальное можно оставить. Файл `.env` читают и compose, и контейнеры; он не попадает ни в образ, ни в git.

```bash
docker compose up -d --build
docker compose ps                                       # backend и web должны стать healthy
```

## 3. Пользователи

Регистрации на сайте нет — пользователей создаёт администратор:

```bash
deploy/cli.sh user add ЛОГИН                            # пароль спросит; первый пользователь — администратор
deploy/cli.sh user list
```

Остальные команды (`passwd`, `disable`, `remove`, `link-code` …) — `deploy/cli.sh user -h`.

## 4. История сообщений и свечи

**История** (отчёты Maverick-ботов) подгружается из вашего Telegram-аккаунта. Первая выгрузка и вход (код придёт в Telegram) делаются один раз, сессия сохранится в томе `data`:

```bash
deploy/cli.sh export data/history.csv --chat 3217240841
deploy/cli.sh export data/history_binance.csv --chat 2119980662
docker compose restart backend                          # дальше сервер сам подгружает новые сообщения: при запуске и каждые HISTORY_SYNC_MIN минут
```

Пути и чаты совпадают с `SOURCE_*_FILE` / `SOURCE_*_CHAT` в `.env`.

**Свечи** докачиваются сами: при запуске backend и далее каждые `CANDLES_SYNC_MIN` минут (первая полная загрузка идёт в фоне, ход — `docker compose logs -f backend` и полоса «Сейчас» на сайте).

**Перенос готовых данных с другого компьютера** (чтобы не качать заново):

```bash
docker compose cp ./data/. backend:/app/data/           # свечи, history*.csv, ui_state.json, signalbot.json, *.session
docker compose exec -u root backend chown -R app:app /app/data
deploy/cli.sh user import-legacy ЛОГИН                  # прежние настройки интерфейса и конфиги бота -> в аккаунт (только если пользователь был один)
```

## 5. HTTPS

```bash
deploy/get-cert.sh your.domain you@mail.com             # [--staging] для пробного выпуска
```

Скрипт записывает `DOMAIN` и `COOKIE_SECURE=true` в `.env`, получает сертификат Let’s Encrypt, перезапускает nginx (он сам включает HTTPS, если сертификат найден) и запускает автопродление. HTTP начинает перенаправлять на HTTPS, добавляется HSTS.

**Без домена** (по IP): оставьте `DOMAIN=_` и `COOKIE_SECURE=false`. Вход по `http://` с `COOKIE_SECURE=true` не работает — браузер не сохраняет Secure-cookie.

## 6. Бот сигналов

1. Создайте бота у @BotFather, токен — в `.env` (`SIGNALBOT_TOKEN`), имя без `@` — в `SIGNALBOT_USERNAME`.
2. `docker compose up -d signalbot` (или `docker compose restart signalbot`).
3. На сайте: меню пользователя → «Привязать Telegram» → отправьте боту `/link КОД`.

Пока токена или пользователей нет, контейнер бота не падает, а ждёт (в логах объяснение). Подробности — `signalbot/README.md`.

## 7. Эксплуатация

| Что | Как |
|---|---|
| Логи | `docker compose logs -f backend` (`signalbot`, `web`); ротация 10 МБ × 5 |
| Обновить сервер | `deploy/update.sh` (git pull, пересборка, перезапуск; данные сохраняются) |
| Резервная копия | `deploy/backup.sh` (полная) или `deploy/backup.sh --small` (без кеша свечей) → `backups/*.tgz`; база в архиве — `maverick.db.bak` |
| Восстановить | остановить сервисы, распаковать архив в том: `docker run --rm -u root -v maverick_data:/data -v $PWD/backups:/in --entrypoint tar maverick-app:latest xzf /in/ФАЙЛ.tgz -C /data`, затем `mv` `maverick.db.bak` → `maverick.db` в томе |
| Перезапуск | `docker compose restart backend` |
| Остановить всё | `docker compose down` (тома остаются; `down -v` удалит и данные!) |
| Сменить пароль | `deploy/cli.sh user passwd ЛОГИН` (все входы пользователя закрываются) |

Рекомендуется раз в сутки запускать `deploy/backup.sh --small` из cron и забирать архив с сервера.

## Безопасность

- Контейнеры backend/бот работают не от root (uid 10001); секреты — только в `.env` на сервере.
- nginx: заголовки безопасности и CSP, `server_tokens off`, ограничение частоты запросов (вход — 10 в минуту на IP, остальной API — 30/с), TLS 1.2+.
- Пароли хранятся хэшем scrypt, сессии — в базе, cookie HttpOnly + SameSite=Lax (+ Secure по HTTPS), защита от перебора пароля и CSRF.
- Файлы с сессией Telegram (`data/*.session`) дают доступ к вашему аккаунту — не копируйте их в открытые места, бэкапы храните приватно.
- Если сервер открыт в интернет, не отключайте `AUTH_ENABLED`.

## Если что-то не работает

| Симптом | Причина / что делать |
|---|---|
| Вход «не держится», после логина снова форма | `COOKIE_SECURE=true`, а сайт открыт по `http://`. Откройте по HTTPS или поставьте `false` |
| 429 при входе | сработало ограничение частоты (nginx или защита от перебора) — подождите минуту |
| В полосе «Сейчас» «автообновление выключено» | не заданы `TG_API_ID`/`TG_API_HASH` или `HISTORY_SYNC=false` |
| «Сессия Telegram не авторизована» | выполните шаг 4 (`export`), потом `docker compose restart backend` |
| Нет свечей / ошибка биржи | `docker compose logs backend`; биржа может блокировать IP дата-центра — смените `EXCHANGE` или сервер |
| Бот молчит | `docker compose logs signalbot`: токен, привязка Telegram (`/link`), есть ли конфиги (`/list`) |
