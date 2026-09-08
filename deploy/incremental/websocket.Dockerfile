ARG BASE_IMAGE=www.gemstory.cn/xianyu/xianyu-websocket:1.0.10
FROM ${BASE_IMAGE}

ARG APP_VERSION=
ARG APP_BUILD_ID=

WORKDIR /app
COPY websocket/requirements.txt /tmp/xianyu-websocket-requirements.txt
RUN old_hash="$(tr -d '\r' < /app/websocket/requirements.txt | sha256sum | cut -d' ' -f1)" \
    && new_hash="$(tr -d '\r' < /tmp/xianyu-websocket-requirements.txt | sha256sum | cut -d' ' -f1)" \
    && if [ "$old_hash" != "$new_hash" ]; then \
        old_playwright="$(python -c 'import importlib.metadata; print(importlib.metadata.version("playwright"))' 2>/dev/null || true)"; \
        pip install --no-cache-dir -r /tmp/xianyu-websocket-requirements.txt; \
        new_playwright="$(python -c 'import importlib.metadata; print(importlib.metadata.version("playwright"))' 2>/dev/null || true)"; \
        if [ "$old_playwright" != "$new_playwright" ]; then playwright install --with-deps chromium; fi; \
    fi \
    && rm -f /tmp/xianyu-websocket-requirements.txt \
    && rm -rf /app/common /app/websocket
COPY common /app/common
COPY websocket /app/websocket
COPY VERSION.txt /app/VERSION.txt
COPY BUILD_ID.txt /app/BUILD_ID.txt
RUN if [ -n "$APP_VERSION" ]; then printf '%s\n' "$APP_VERSION" > /app/VERSION.txt; fi \
    && if [ -n "$APP_BUILD_ID" ]; then printf '%s\n' "$APP_BUILD_ID" > /app/BUILD_ID.txt; fi
