ARG BASE_IMAGE=www.gemstory.cn/xianyu/xianyu-frontend:1.0.10

FROM node:20-alpine AS build
ARG APP_VERSION=
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund \
    --fetch-retries=5 \
    --fetch-retry-factor=2 \
    --fetch-retry-mintimeout=1000 \
    --fetch-retry-maxtimeout=30000
COPY frontend ./
RUN if [ -n "$APP_VERSION" ]; then APP_VERSION="$APP_VERSION" npm run build; else npm run build; fi

FROM ${BASE_IMAGE}
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build /src/dist /usr/share/nginx/html
