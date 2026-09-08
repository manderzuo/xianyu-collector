# Windows Docker 自动部署说明

## 一、使用前提

目标电脑只需要提前安装并启动 Docker Desktop。部署脚本会自动检查 Docker CLI、Docker Compose 和 Docker 引擎状态。

将整个项目文件夹复制到目标电脑任意目录，例如桌面、D 盘或其他数据盘均可。脚本不会依赖固定盘符、固定用户名或固定工作目录。

## 二、首次安装

双击项目根目录下的 `install.bat`。

脚本会自动完成：

1. 识别项目根目录。
2. 创建本地 `.env` 配置文件。
3. 生成本机独立的容器、网络和数据卷名称。
4. 检测端口占用，并自动选择可用端口。
5. 构建前端、后端、WebSocket 和 Scheduler 镜像。
6. 启动 MySQL、Redis、Backend、WebSocket、Scheduler 和 Frontend。
7. 等待前端响应。
8. 在当前 Windows 用户桌面创建 `闲鱼管理系统` 启动器，并设置闲鱼风格图标。
9. 自动打开浏览器。

如果 `.env` 中 `XR_DEPLOY_MODE=remote`，安装脚本会从更新清单指定的 GHCR 镜像拉取镜像，不会在客户电脑上编译源码。

制作离线包时请把输出目录显式指定到数据盘，例如 `D:\xianyu-release`。离线镜像导出产生的大型临时 tar 文件默认也会放在这个包目录所在磁盘，完成后自动清理；如需指定其他磁盘，可使用 `-OfflineTempDirectory`。

## 三、后续启动

以后可以双击桌面上的 `闲鱼管理系统`，或者双击项目根目录的 `start.bat`。启动器会按当前 `.env` 的端口启动已有容器并打开浏览器。

停止服务可以双击 `stop.bat`。该操作只停止容器，不删除数据库、Redis、上传文件或浏览器数据。

## 四、端口和旧环境保护

首次安装默认尝试使用 20000、28089、28090、28091；如果端口被占用，脚本会继续向后寻找可用端口，并将最终端口写入项目目录的 `.env`。

脚本不会使用或覆盖旧稳定端口 19000，也不会执行 `docker compose down -v`、删除数据卷或删除旧容器的操作。

如果目标电脑已经运行另一套系统，项目会使用自己的容器前缀、网络名称和数据卷名称，避免因固定容器名称导致冲突。

## 五、GitHub 免费镜像和自动更新

源码仓库可以继续保持私有。每次推送到 `main`、创建版本标签或手动运行 GitHub Actions 时，都会读取仓库的 `VERSION.txt`，在 GitHub Container Registry（GHCR）完成构建，再通过 SSH 将镜像导入腾讯云自建的只读分层仓库。客户端维护包和 `release/latest.json` 仍由腾讯云 HTTPS 站点发布。连续推送同一版本时，会使用新的构建号触发客户端更新。

客户端启动器会读取 `.env` 中的 `UPDATE_MANIFEST_URL`。发现新版本后弹窗提示，用户确认后执行：

1. 保存当前 `.env` 备份。
2. 切换到清单中的镜像仓库和镜像标签。
3. 从 `www.gemstory.cn` 拉取镜像；Docker 自动复用本机已有层，只下载变化层。
4. 使用原有数据卷重启服务。
5. 检查前端健康状态。
6. 成功后写入新的版本号和构建号。

更新失败会恢复 `.env` 并尝试启动原镜像。更新不会删除数据库、Redis、上传文件或浏览器数据。

客户电脑不再直接访问 GHCR，也不下载完整镜像压缩包。腾讯云 Nginx 只允许公开的 Docker Registry
`GET`/`HEAD` 拉取请求，写入只允许服务器本机通过 SSH 导入，不需要在客户电脑保存仓库密码。

镜像仓库地址、命名空间、更新清单地址和 Token 均从 `.env` 或部署配置读取，不写死盘符和本地目录。

GitHub Actions 只需要配置腾讯云更新目录相关 Secrets：`TENCENT_UPDATE_SSH_HOST`、`TENCENT_UPDATE_SSH_USER`、`TENCENT_UPDATE_SSH_KEY`、`TENCENT_UPDATE_REMOTE_PATH`。镜像发布使用 GitHub 自动提供的 `GITHUB_TOKEN`，腾讯云分层仓库由 `deploy/registry/install.py` 自动安装和维护，不需要购买 TCR，也不需要额外仓库密码。

云端注册审批和客户端诊断服务通过单独的 `Deploy Xianyu cloud auth service` 工作流部署。进入仓库的 `Actions`，选择该工作流，点击 `Run workflow`，填写需要部署的分支、标签或提交号，然后等待服务健康检查通过。该工作流使用上面相同的腾讯云 SSH Secrets，不会覆盖云端认证数据库、会话密钥或诊断加密密钥。

## 六、数据迁移

只复制代码文件不会自动带走原电脑中的 Docker 数据卷。若需要迁移原系统数据，应先在原电脑做数据库备份，再在目标电脑恢复；不要直接复制正在使用中的 MySQL 数据目录。

需要迁移的敏感数据包括：数据库备份、上传文件、浏览器登录数据和 `.env`。其中 `.env` 可能包含数据库密码、JWT 密钥和外部 API Key，不要公开发送。

## 七、故障排查

### Docker 未启动

打开 Docker Desktop，等待状态变为 Running，再重新运行安装脚本。

### 页面打不开

查看项目目录 `.env` 中的 `FRONTEND_PORT`，在浏览器访问 `http://127.0.0.1:<FRONTEND_PORT>`。

### 端口被占用

删除 `.env` 前请先备份。通常不需要手动改端口；重新运行安装脚本会检查当前配置并在端口冲突时自动选择新端口。不要删除 Docker 数据卷。

### 服务启动失败

在项目根目录打开 PowerShell，执行：

```powershell
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=100 backend
docker compose --env-file .env logs --tail=100 frontend
```

重点检查 Backend 健康状态、MySQL 健康状态、Redis 健康状态和前端端口。

## 八、脚本文件说明

| 文件 | 作用 |
| --- | --- |
| `install.bat` | 首次安装、构建、启动和创建桌面启动器 |
| `start.bat` | 启动现有部署并打开浏览器 |
| `stop.bat` | 停止当前部署，不删除数据 |
| `scripts/install.ps1` | 安装主逻辑 |
| `scripts/start.ps1` | 启动逻辑 |
| `scripts/stop.ps1` | 停止逻辑 |
| `app/deploy/check-xianyu-update.ps1` | 检查并执行镜像更新 |
| `.env` | 当前电脑的实际端口、密钥和部署标识 |

三个 `.bat` 文件只使用英文、数字和 ASCII 符号，避免 Windows 批处理文件因中文编码产生执行错误。
