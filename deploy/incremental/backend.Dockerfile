ARG BASE_IMAGE=www.gemstory.cn/xianyu/xianyu-backend:1.0.10
FROM ${BASE_IMAGE}

ARG APP_VERSION=
ARG APP_BUILD_ID=

WORKDIR /app
COPY backend/requirements.txt /tmp/xianyu-backend-requirements.txt
RUN old_hash="$(tr -d '\r' < /app/backend/requirements.txt | sha256sum | cut -d' ' -f1)" \
    && new_hash="$(tr -d '\r' < /tmp/xianyu-backend-requirements.txt | sha256sum | cut -d' ' -f1)" \
    && if [ "$old_hash" != "$new_hash" ]; then \
        pip install --no-cache-dir -r /tmp/xianyu-backend-requirements.txt; \
    fi \
    && rm -f /tmp/xianyu-backend-requirements.txt \
    && rm -rf /app/common /app/backend
COPY common /app/common
COPY backend /app/backend
COPY dialogue_packs /app/dialogue_packs
COPY VERSION.txt /app/VERSION.txt
COPY BUILD_ID.txt /app/BUILD_ID.txt
RUN if [ -n "$APP_VERSION" ]; then printf '%s\n' "$APP_VERSION" > /app/VERSION.txt; fi \
    && if [ -n "$APP_BUILD_ID" ]; then printf '%s\n' "$APP_BUILD_ID" > /app/BUILD_ID.txt; fi \
    && mkdir -p /app/static/uploads /app/backups
