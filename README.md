# xianyu-rewrite — 闲鱼自动化系统

> 全新仓库，业务模块按当前功能重新实现。扫码登录按独立闲鱼协议实现，品牌和域名保留为可配置项。

## 功能清单（全部覆盖）
- 多账号管理（扫码登录、Cookie 维护、续期、状态、代理）
- 消息与自动回复（关键词/图片/默认/商品专属 + AI 透传）、在线聊天、快捷短语
- 商品发布（素材库、地址库、单发/批量、发布日志、发布能力探测）
- 订单与售后（拉单、自动评价、求小红花、超时检测、退款/取消）
- 采集与监控（Goofish 采集、货源、分销、指南针、选品监控）
- 通知与风控（通知渠道、风控日志、系统公告/弹窗/广告）
- 文件与上传、数据分析、系统设置、定时任务中心（20+ 任务可视化 + 手动触发）
- 外部对接（external_*）、共享扫码、二维码、人脸核验、支付

当前版本已完成内部业务链路：鉴权、账号、资源 CRUD、真实扫码登录、商品搜索/采集、
动作记录、排名、上传、统计和定时任务调度均使用真实数据库与服务调用。需要第三方
支付/核验或尚未接入平台连接器的动作，会写入任务记录并返回明确的
`waiting_for_connection` / `unavailable` 状态；配置连接后再执行，不把未实际完成的
外部动作伪装成成功。详细差异见 `docs/功能对照审计.md`。

## 技术栈
- 后端：Python 3.11 + FastAPI + SQLAlchemy 2.0 + asyncmy + Redis 7 + APScheduler
- 自动化：Playwright / Patchright（仅发布/验证场景，接口场景走 mtop 直连）
- 前端：React 18 + TypeScript + Vite + TailwindCSS + Zustand
- 基建：MySQL 8 + Redis 7 + Nginx
- 部署：`docker-compose.yml` 一键起；`frontend/dist` 直上 Cloudflare Pages
- WebSocket：客户端直连 `ws://<host>:8090/api/v1/ws`（本地验收因端口占用使用 `18090` 映射）

## 目录
```
backend/        # backend-web 服务
websocket/      # 长连接服务
scheduler/      # 定时任务服务
common/         # 共享：db / models / schemas / utils
frontend/       # React 前端（全新设计）
deploy/         # nginx + CF Functions 代理
docs/           # 文档
```

## 快速开始（本地隔离测试）
```bash
# 1. 配置
cp .env.example .env  # 按需改密码/端口

# 2. 一键起后端
docker compose up -d --build
curl http://127.0.0.1:8089/health
curl http://127.0.0.1:8090/health
curl http://127.0.0.1:8091/health

# 3. 前端
cd frontend && npm ci && npm run dev   # http://127.0.0.1:9000
# 或打包预览（等同 CF 线上）
npm run build && npm run preview -- --host 0.0.0.0 --port 9001
```
管理员账号由首次部署初始化，登录后请立即修改密码。

## Windows 自动部署

将整个项目目录复制到目标电脑并启动 Docker Desktop 后，双击项目根目录的
`install-xianyu.bat`。脚本会以自身目录为项目根目录，自动创建 `.env`、选择未占用端口、
构建并启动前后端及依赖服务，并在当前用户桌面创建 `Xianyu System` 启动器。

脚本不会写死盘符或用户目录，不会执行删除数据卷的操作，也不会覆盖旧稳定端口 19000。
后续双击桌面启动器或 `start-xianyu.bat` 即可启动已有部署；需要停止服务时使用
`stop-xianyu.bat`。详细说明见 `docs/Windows-Docker-自动部署说明.md`。

## GitHub 免费镜像更新

项目支持本地构建和远程镜像两种模式。GitHub Actions 使用 GitHub Container Registry（GHCR）
构建并发布四个公开镜像，腾讯云服务器只托管更新清单。客户电脑可以在 `.env` 中设置
`XR_DEPLOY_MODE=remote`，无需 Docker 登录即可拉取镜像。桌面启动器会检查
`UPDATE_MANIFEST_URL`，发现新版本后提示确认，再拉取新镜像并重启，不会删除本地数据卷。
GitHub Actions 配置见 `.github/workflows/build-and-publish.yml`。

公开镜像不需要额外购买腾讯云 TCR，但镜像内的程序文件可以被公开拉取；如果后续需要保护镜像内容，
再切换到私有镜像仓库并为客户端配置只读访问权限。

## Cloudflare 隔离部署（前端上 CF Pages，后端走临时托管）
- 构建：`frontend` Root=`frontend` Build=`npm ci && npm run build` Output=`dist`
- 环境变量：`BACKEND_URL=https://你的后端公网地址`（Functions 代理 `/api` `/static` 到后端）
- 参考：`deploy/README.md` 与 `deploy/functions/`

## 品牌/域名占位
- 全仓库以 `BRAND_NAME` / `BRAND_DOMAIN` 占位，待你补充后全局替换即可。
- 不含任何原仓库远程地址、二维码、激活逻辑。
