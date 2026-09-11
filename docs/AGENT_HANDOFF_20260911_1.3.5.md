# 闲鱼管理系统 Agent 交接文档

## 1. 交接结论

当前最新版本为 **1.3.5 综合修复版**，已经完成代码推送、镜像构建、客户端维护包上传、签名清单发布和线上可读性校验。

- 版本：`1.3.5`
- Git 标签：`v1.3.5`
- Git 提交：`019ee59de32af6a121d8649cac2119b3b0d714db`
- 构建 ID：`019ee59de32a-1.3.5`
- 发布流水线：[GitHub Actions 34563009082](https://github.com/manderzuo/xianyu-collector/actions/runs/34563009082)

1.3.5 已经可以作为线上测试版本使用。

## 2. 最新代码地址

代码仓库：

`https://github.com/manderzuo/xianyu-collector.git`

最新发布标签：

`https://github.com/manderzuo/xianyu-collector/releases/tag/v1.3.5`

当前发布分支：

`codex/release-1.3.3-client-handoff-fix`

注意：当前 `origin/main` 不是最新的 1.3.5 发布提交。继续开发时必须从 `v1.3.5` 或上述发布分支开始，不要直接从旧的 `main` 开始。

推荐操作：

```powershell
git clone https://github.com/manderzuo/xianyu-collector.git
cd xianyu-collector
git fetch --all --tags
git checkout -b codex/next-task v1.3.5
```

如果已经存在本地仓库：

```powershell
git fetch origin --tags
git switch codex/release-1.3.3-client-handoff-fix
git pull --ff-only origin codex/release-1.3.3-client-handoff-fix
```

## 3. 正确工作树

本次 1.3.5 最新源代码所在工作树：

`C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite`

特别注意：

`C:\Users\StarLink\.codex\worktrees\aa16\xianyu-rewrite` 是较旧的工作树，曾经停留在 1.3.0 相关分支，不能作为最新代码来源。

工作树中存在一些历史遗留的未跟踪临时文件和测试素材，包括 `artifacts/`、`.tmp-lucide/`、`pelican-bicycle.html` 等。这些文件不属于 1.3.5 发布内容，不要执行清理，也不要提交到新版本。

`E:\xianyu` 是本机 Docker 运行测试目录，不是代码仓库的权威来源。该目录曾经做过临时运行时修复，不能反向覆盖 Git 源码。

## 4. 线上更新地址

更新清单：

`https://www.gemstory.cn/release/xianyu/latest.json`

清单签名：

`https://www.gemstory.cn/release/xianyu/latest.json.sig`

线上 Registry：`www.gemstory.cn/xianyu`

四个业务镜像：

```text
www.gemstory.cn/xianyu/xianyu-backend:1.3.5
www.gemstory.cn/xianyu/xianyu-websocket:1.3.5
www.gemstory.cn/xianyu/xianyu-scheduler:1.3.5
www.gemstory.cn/xianyu/xianyu-frontend:1.3.5
```

1.3.5 客户端维护包：

`https://www.gemstory.cn/release/xianyu/xianyu-client-019ee59de32a-1.3.5.zip`

客户端包 SHA-256：`c9612eb71663b6a02822b8ee75fcfebbe22a69d2f92d9549685de2c0e4fdfdb1`

启动器 SHA-256：`7a52f500d7458f6e51ca0766032e1781a52d86fbf61178cc6738eabcf1da4c1f`

前端入口 `index.html` SHA-256：`ed160654354377fabb54a6372b88ec744ee8e594089c1e33b709e7904a128363`

## 5. 1.3.5 已完成的修复

### 5.1 云端系统登录鉴权

- 修复云端用户名密码登录成功后，刷新页面或刷新令牌又跳回登录页的问题。
- 云端登录成功后，将云端会话令牌正确写入本地访问令牌和刷新令牌声明。
- 刷新令牌时继续保留云端会话信息。
- 本地用户表只作为系统用户镜像，不再把闲鱼账号登录信息当作云端账号数据保存。
- 云端邮件验证码登录在当前模式下给出明确提示，避免伪装成成功登录。

主要文件：`backend/app/api/routes/auth.py`、`backend/tests/test_cloud_auth_login.py`。

### 5.2 禁止闲鱼账号 Cookie/Token 上传云端

云端鉴权服务已对以下账号会话同步接口做服务端阻断，返回 HTTP 410：

- `list_account_sessions`
- `sync_account_session`
- `get_account_session`
- `delete_account_session`

主要文件：`deploy/cloud_auth/server.py`。

注意：`deploy/cloud_auth/` 是独立云端鉴权服务。如果线上鉴权服务不是通过当前主流水线自动部署，需要单独将该目录的服务代码部署到云端，才能让云端接口阻断逻辑生效。主应用后端镜像中的登录修复已经随 1.3.5 发布。

### 5.3 更新流程修复

- 检测到新版本后自动进入下载和安装，不再要求用户手动点击“立即更新”。
- 客户端维护包不再被错误报告为最终完成，而是发送 `restart_client` 状态。
- 更新器自动关闭并启动新版主启动器。
- 启动器应用客户端包后，继续执行延迟的 Docker 运行时镜像同步。
- 修复“界面已经是新版本，但 Docker 镜像仍是 1.2.0”的分裂状态。
- 修复更新完成后进度条持续滚动、不退出、不提示的问题。
- 修复检查阶段回滚徽标长期显示“回滚保护：检测中”。
- 修复遗留 `runtime-sync.pending.json` 导致每次启动重复提示、停在 2/6 的问题。
- 兼容从 1.2.0、1.3.0、1.3.1、1.3.2、1.3.3 和 1.3.4 升级。

主要文件：`deploy/update-xianyu-gui.ps1`、`tools/launcher/XianyuLauncher.cs`、`tools/launcher/XianyuLauncherCore.cs`、`tools/windows-installer-apply-client-update.ps1`、`tools/windows-installer-start.ps1`、`.github/workflows/build-and-publish.yml`。

### 5.4 内置 AI 自动回复

1.3.5 继承 1.3.4 的内置 AI 自动回复修复：

- `builtin_ai_reply_enabled` 可以真正保存到数据库。
- 刷新页面后开关状态不会恢复为关闭。
- VIP 或管理员开启后，关键词列表自动加载预置规则。
- 预置关键词和话术不需要普通用户手动逐条添加。
- 测试环境验证过内置规则加载数量为 336 条。

主要文件：`backend/app/api/routes/ai_reply.py`、`backend/app/services/account_settings.py`、`dialogue_packs/distilled/`。

## 6. 旧版本升级流程

1.2.0 及早期版本使用“客户端优先”的增量更新方式。线上 1.3.5 清单当前有意设置为：

```json
{
  "runtime_images_required": false,
  "runtime_images_deferred": true
}
```

这不是镜像缺失，而是为了兼容旧客户端，先下载并应用客户端维护包，再自动同步运行时镜像。

完整流程：

1. 旧客户端检测到 1.3.5。
2. 自动下载并校验客户端维护包。
3. 更新器发送 `restart_client`，自动启动新版启动器。
4. 新启动器应用客户端维护包。
5. 客户端包中的 `runtime-sync.pending.json` 保留运行时同步任务。
6. 新更新器自动同步四个 1.3.5 业务镜像。
7. 更新 `.env` 中的部署模式、Registry、Namespace 和 Tag。
8. 重建并校验四个业务容器。
9. 通过前端健康检查后，清理运行时同步标记并报告最终完成。

验收时不能只看界面版本，必须同时检查：

```powershell
Get-Content D:\xianyu\app\VERSION.txt
Get-Content D:\xianyu\app\BUILD_ID.txt
Get-Content D:\xianyu\app\.env | Select-String 'XR_DEPLOY_MODE|XR_IMAGE_REGISTRY|XR_IMAGE_NAMESPACE|XR_IMAGE_TAG'
docker compose --project-directory D:\xianyu\app --env-file D:\xianyu\app\.env -f D:\xianyu\app\docker-compose.yml ps
```

最终应看到 `VERSION.txt=1.3.5`、运行时 Tag 为 `1.3.5`、四个容器使用 1.3.5 镜像，并且日志包含运行时镜像校验成功记录。

## 7. 关键配置和安全边界

- 系统登录密码用于云端系统鉴权，不能与闲鱼账号 Cookie/Token 混淆。
- 闲鱼账号登录数据必须留在本地运行目录或 Docker 数据卷中。
- 不要把 `.env`、账号 Cookie、Token、浏览器数据、日志敏感内容提交到 Git。
- 发布清单必须使用 RSA-SHA256 签名。
- 客户端内置公钥文件为 `deploy/update-signing-public-key.xml`。
- 线上发布必须先上传客户端包和签名，再最后上传 `latest.json`。

## 8. 本轮验证结果

已完成：

- PowerShell 更新脚本语法解析通过。
- C# 桌面启动器编译通过。
- 前端 `npm run build` 通过，构建版本为 1.3.5。
- Python `compileall` 通过。
- GitHub Actions 1.3.5 全流程成功。
- 线上 `latest.json` 已返回 1.3.5。
- 线上签名文件 HTTP 200。
- 客户端维护包 HTTP 200。
- 四个 1.3.5 镜像 Registry 检查均 HTTP 200。

当前环境没有安装 `pytest`，因此本轮没有重新执行后端 pytest；此前云端登录修复测试已在本分支执行通过。下一个 Agent 若要继续后端测试，应先在隔离虚拟环境中安装项目测试依赖，再执行：

```powershell
python -m pytest backend/tests -q
```

## 9. 尚未完成的工作

### 9.1 VIP/套餐权限独立界面

用户要求 VIP/套餐权限必须保持独立，不要整合进用户管理。当前代码状态：

- 侧边栏已有独立菜单：`套餐权限`。
- 页面路由为 `/admin/entitlements`。
- 后端已有独立套餐和用户授权接口。
- 但前端套餐权限页仍有跳转用户管理的提示。
- `UserFormModal.tsx` 中仍保留用户套餐字段编辑。

后续 Agent 需要把用户 VIP 开通、套餐变更和功能授权全部收回 `/admin/entitlements` 页面，并将用户管理中的套餐编辑入口删除或改为只读提示。该项没有并入 1.3.5。

### 9.2 发布完成后的持续验收

建议使用一台 1.2.0 测试机和一台 1.3.4 测试机分别验证：

- 是否自动开始更新。
- 是否自动重启启动器。
- 是否最终同步到 1.3.5 Docker 镜像。
- 是否不再卡在回滚保护检测中。
- 是否不再卡在 2/6。
- 是否保留系统登录状态。
- VIP 用户开启内置 AI 后是否出现 336 条预置关键词。

## 10. 后续发布规范

下一个版本建议从 `v1.3.5` 创建分支，不要直接改线上 Tag：

```powershell
git fetch origin --tags
git checkout -b codex/next-release v1.3.5
```

修改版本时同步更新：

- `VERSION.txt`
- `BUILD_ID.txt`
- `frontend/package.json`
- `frontend/package-lock.json`
- 发布说明文档

发布前执行：

```powershell
npm ci --prefix frontend
$env:APP_VERSION='下一个版本号'
npm run build --prefix frontend
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-launcher.ps1 -OutputDirectory C:\Temp\xianyu-launcher-check -Force
```

提交并发布标签：

```powershell
git add VERSION.txt BUILD_ID.txt frontend/package.json frontend/package-lock.json deploy tools docs
git commit -m "release: publish Xianyu 下一个版本号"
git tag -a v下一个版本号 -m "Xianyu 下一个版本号"
git push origin HEAD:对应发布分支
git push origin v下一个版本号
```

不要使用 `git reset --hard`、`git clean -fd` 或其他会删除未跟踪文件的命令，除非已经确认临时文件属于当前 Agent。
