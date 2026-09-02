# 部署说明

## Docker

`docker compose up -d --build` 会构建前端静态资源并由 Nginx 提供访问，`/api` 和 `/static` 反向代理到后端。

## Cloudflare Pages

构建目录使用 `frontend`，构建命令为 `npm ci && npm run build`，输出目录为 `dist`。在 Pages 环境变量中配置 `BACKEND_URL` 为后端公网地址，`functions/` 中的代理会保留请求方法、请求体和查询参数。
