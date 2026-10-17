# Сборка фронтенда и nginx, который раздаёт его и проксирует /api на backend.
FROM node:20-alpine AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /src/dist /usr/share/nginx/html
COPY docker/nginx/templates /etc/nginx/templates-src
COPY docker/nginx/snippets /etc/nginx/snippets
COPY docker/nginx/10-select-mode.sh /docker-entrypoint.d/10-select-mode.sh
COPY docker/nginx/15-reload-loop.sh /docker-entrypoint.d/15-reload-loop.sh
RUN chmod +x /docker-entrypoint.d/10-select-mode.sh /docker-entrypoint.d/15-reload-loop.sh && rm -f /etc/nginx/conf.d/default.conf
# DOMAIN подставляется в server_name и путь к сертификату; "_" — любой хост (доступ по IP, без TLS)
ENV DOMAIN=_
