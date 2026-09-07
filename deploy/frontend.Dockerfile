FROM node:20-alpine AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json* ./
# Use the committed lockfile for reproducible builds. Registry requests can be
# transient on hosted runners, so retry package downloads before failing.
RUN npm ci --no-audit --no-fund \
    --fetch-retries=5 \
    --fetch-retry-factor=2 \
    --fetch-retry-mintimeout=1000 \
    --fetch-retry-maxtimeout=30000
COPY frontend ./
RUN npm run build

FROM nginx:alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
