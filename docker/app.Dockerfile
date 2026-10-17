# Один образ для backend (FastAPI) и бота сигналов: код общий (бот импортирует движок из backend).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# зависимости отдельным слоем: пересобираются только при смене requirements
COPY backend/requirements.txt backend/requirements.txt
COPY signalbot/requirements.txt signalbot/requirements.txt
RUN pip install -r signalbot/requirements.txt

# код; данные (кеш свечей, история сообщений, база, сессия Telegram) живут в томе /app/data
COPY backend/ backend/
COPY signalbot/ signalbot/

RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/data \
    && chown app:app /app/data
USER app

# ROOT репозитория внутри контейнера — /app: data/ и файлы сессий Telethon лежат там же, где в обычной установке
VOLUME ["/app/data"]
WORKDIR /app/backend
EXPOSE 8000
# 1 воркер обязателен: фоновые задачи (подгрузка истории и свечей, очередь загрузок) живут в процессе
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header"]
