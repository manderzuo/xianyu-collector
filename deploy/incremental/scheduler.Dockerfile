ARG BASE_IMAGE=www.gemstory.cn/xianyu/xianyu-scheduler:1.0.10
FROM ${BASE_IMAGE}

ARG APP_VERSION=
ARG APP_BUILD_ID=

WORKDIR /app
COPY scheduler/requirements.txt /tmp/xianyu-scheduler-requirements.txt
RUN old_hash="$(tr -d '\r' < /app/scheduler/requirements.txt | sha256sum | cut -d' ' -f1)" \
    && new_hash="$(tr -d '\r' < /tmp/xianyu-scheduler-requirements.txt | sha256sum | cut -d' ' -f1)" \
    && if [ "$old_hash" != "$new_hash" ]; then \
        pip install --no-cache-dir -r /tmp/xianyu-scheduler-requirements.txt; \
    fi \
    && rm -f /tmp/xianyu-scheduler-requirements.txt \
    && rm -rf /app/common /app/scheduler
COPY common /app/common
COPY scheduler /app/scheduler
COPY VERSION.txt /app/VERSION.txt
COPY BUILD_ID.txt /app/BUILD_ID.txt
RUN if [ -n "$APP_VERSION" ]; then printf '%s\n' "$APP_VERSION" > /app/VERSION.txt; fi \
    && if [ -n "$APP_BUILD_ID" ]; then printf '%s\n' "$APP_BUILD_ID" > /app/BUILD_ID.txt; fi
