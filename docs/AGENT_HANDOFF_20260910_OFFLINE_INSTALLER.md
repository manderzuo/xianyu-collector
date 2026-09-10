# 闲鱼管理系统 Agent 交接文档

更新时间：2026-09-11（UI 第二轮：控制台微调、DPI 四档、品牌图标统一，见 §14）  
交接主题：以当前最新代码为基础继续开发 Windows 离线安装包、Docker 镜像归档和增量更新流程；UI 三界面重做已完成并全状态验证，剩余事项见 §14 末尾清单

## 0. 基线与重要提醒

请在下面的工作树继续工作：

~~~text
C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite
~~~

当前基线是 detached HEAD，提交为 57da30c（origin/main 当前指向的最新提交）。当前工作树存在有意保留的未提交和未跟踪改动，**不要执行 git reset --hard、git checkout --、清理未跟踪文件或覆盖这些改动**。（注：截至 §14 本轮，工作树 HEAD 已前进到 f322ecc，未提交清单以 §14"当前工作区状态"为准。）

另一个工作树 C:\Users\StarLink\.codex\worktrees\aa16\xianyu-rewrite 是另一条业务开发线，不是本交接的安装包基线；不要把它的业务改动混入本任务。

用户明确要求：

1. 继续以当前最新代码为基础开发。
2. 每个版本生成新的离线镜像后，每个服务只保留当前版本的一份归档。
3. 新版本完整生成、校验成功前，不得删除或破坏旧的 .tar.gz；失败时必须保留旧包。
4. 离线安装和离线修复不能偷偷执行 Docker 镜像网络拉取。
5. 未经用户确认，不推送线上、不发布新版本、不覆盖正式安装包。

## 1. 当前版本与代码基线

~~~text
产品：xianyu-rewrite
版本：1.2.0
BUILD_ID：20260909-1.2.0-incremental-runtime-sync
Git HEAD：57da30c fix: preserve flattened offline image base chain
Git 状态：存在未提交改动和未跟踪文件，见第 3 节
~~~

最近的发布/构建相关提交：

- 57da30c：修复 flattened offline image 的基础层链保留。
- 6b766a5：优先使用缓存的 GHCR runtime base。
- 41fc84d：重建不完整的 release image set。
- b330a5b：使部署健康检查安全重试。
- 8ad27f8：发布 Xianyu 1.2.0 增量更新，包含 Windows 安装器、更新器和离线导入基础实现。

## 2. 系统设计

### 2.1 Compose 镜像命名

docker-compose.yml 中：

- MySQL：mysql:8.0
- Redis：redis:7-alpine
- 应用服务：XR_IMAGE_REGISTRY/XR_IMAGE_NAMESPACE/xianyu-服务名:XR_IMAGE_TAG

远程发布默认使用：

~~~text
registry：www.gemstory.cn
namespace：xianyu
tag：版本号，例如 1.2.0
~~~

离线安装清单使用：

~~~text
registry：local
namespace：xianyu
tag：版本号
~~~

远程更新清单：

~~~text
https://www.gemstory.cn/release/xianyu/latest.json
~~~

若启用签名，签名地址为同域的 latest.json.sig，客户端公钥位于 deploy/update-signing-public-key.xml。

### 2.2 离线镜像构建链路

正式的 -IncludeDockerImages 打包路径现在是：

~~~text
build-windows-installer.ps1
    ├─ build-offline-registry-bundle.ps1
    │    └─ 从 Registry manifest/config/layer blobs 构建 OCI 归档
    └─ add-offline-infrastructure-bundle.ps1
         └─ 导出 mysql:8.0 和 redis:7-alpine
~~~

不要把 Docker Desktop containerd 下的普通 docker save 结果直接当作业务镜像归档的唯一来源。旧路径在某些环境可能产生只有元数据、缺少真实 layer blob 的坏包。Registry bundle 会保留原始 registry layer digest，供 Docker Desktop 导入和后续增量复用。

build-offline-image-bundle.ps1 仍然保留，用于本地已有镜像的兼容导出或特殊场景；正式打包器在 IncludeDockerImages 时应优先调用 Registry bundle，而不是回退到旧的本地 docker save 路径。

### 2.3 归档提交策略

以下三个脚本已经改成临时输出目录模式：

- tools/build-offline-registry-bundle.ps1
- tools/build-offline-image-bundle.ps1
- tools/add-offline-infrastructure-bundle.ps1

基本流程：

1. 在 package 目录下的临时工作目录生成所有 tar 和 .tar.gz。
2. 对每个归档进行大小、内容、layer blob 和 SHA-256 校验。
3. 在临时目录写完整 offline-manifest.json。
4. 全部成功后，把旧目标文件移入临时备份，再提交新归档和清单。
5. 提交阶段发生异常时，按备份恢复旧文件。
6. 只有成功提交后，才清理当前脚本负责的旧版本归档。

Registry application builder 只清理 backend/websocket/scheduler/frontend 归档；MySQL 和 Redis 由 infrastructure builder 负责。这样当基础镜像生成失败时，不会先把旧的基础镜像归档清掉。

## 3. 当前未提交改动

### 3.1 已修改的跟踪文件

- deploy/start-xianyu.ps1：读取 XR_DEPLOY_MODE，offline 模式启动 Compose 时增加 --pull never。
- deploy/update-xianyu-gui.ps1：远程 manifest、签名、版本/Build、远程 digest、本地镜像和容器镜像 ID 校验；只拉取变化服务；尽量复用上一版本相同 digest 的本地镜像；重启和失败回滚使用 --pull never；客户端维护包先暂存，下一次启动再替换。
- tools/build-offline-image-bundle.ps1：记录 source image ID、registry digest、rootfs layer IDs；导出结果先写临时目录，再提交。
- tools/build-windows-installer.ps1：IncludeDockerImages 时调用 Registry bundle builder，再调用基础镜像 builder；将离线修复脚本、注入 BAT 和清单放进安装包。
- tools/import-offline-image-bundle.ps1：校验 archive SHA-256；处理 Docker Desktop/containerd 大镜像导入后标签短暂不可见的竞态；必要时按 immutable image ID 重新挂载目标标签。
- tools/validate-commercial-delivery.ps1：使用 UTF-8 读取 README；使用 ParseInput 检查 PowerShell 语法；检查新增离线脚本、归档数量、归档路径安全性和 SHA-256。
- tools/windows-installer-install.ps1：离线安装使用 docker compose up -d --no-build --pull never。
- tools/windows-installer-repair-offline-install.ps1：离线修复启动使用 --pull never。
- tools/windows-installer-start.ps1：正常启动在 offline 模式使用 --pull never。

### 3.2 当前未跟踪文件

这些文件是本交接的一部分，不要删除：

- tools/add-offline-infrastructure-bundle.ps1
- tools/build-offline-registry-bundle.ps1
- tools/windows-installer-inject-offline-images.bat
- tools/windows-installer-repair-legacy-update.ps1
- tools/windows-installer-repair-offline-runtime.ps1
- artifacts/repair-legacy-update.ps1
- artifacts/repair-legacy-update.zip

其中 artifacts/repair-legacy-update.* 是旧安装包兼容修复工具的发布产物，除非确认其已被替代并得到用户授权，否则不要清理。

## 4. 已生成并验证的本地安装包

安装包目录：

~~~text
D:\本地安装包
~~~

包清单：

~~~text
版本：1.2.0
类型：windows-portable-docker
安装入口：安装闲鱼管理系统.exe
兼容入口：install.bat
启动入口：start.bat
离线清单：resources\images\offline-manifest.json
基础镜像是否包含：是
应用镜像是否延迟：否
~~~

离线清单是 format version 2，包含 6 个归档：

| 服务 | 归档 | SHA-256 | source image ID |
|---|---|---|---|
| backend | backend.tar.gz | bfb0bc29455c5fac6c202896f27cd68d080708e1fde72a491d07454b049ff3c5 | sha256:74010e466a17e6c4eaf155bd076d186dbcda49c270aa1efb5e9cc94a28ad9b0a |
| websocket | websocket.tar.gz | 48d0a2be3bad9d157af69e5a8f8bb6bd6bfcb2ed27870bee19d7c95e02d5c3f7 | sha256:1eb472b8ff30d26eb23a04cc24244c69141d13eec18afe9940263abdccc1bc7c |
| scheduler | scheduler.tar.gz | 578ecdcb5e06f52fa2a70ddd01ddb7927fd5a1d68fc254e9c99095d9ecb566c1 | sha256:1e71bff121a71653fe88ae5762f526964630ab0e953ab1446fe2563e5720fce9 |
| frontend | frontend.tar.gz | d48a19c828c4d18a45fe9eae1caabe3acb4272874db9eee66600542849188928 | sha256:3864ec626f28a757ef69210624a1b31fadeb610b305d2c509257fb44cd57f9cb |
| mysql-8.0 | mysql-8.0.tar.gz | e8526ef25aed4f30ae56ef58591220be1774914be48872cd665b2e7b8fb97322 | sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b |
| redis-7-alpine | redis-7-alpine.tar.gz | ec4fe0a77d19dc38ccd37cdc53e5d078e7346f5f94002b5073565280a59496c1 | sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99 |

已知安装包行为：

- 安装闲鱼管理系统.exe 会打开安装 GUI；点击“下一步”后才真正执行安装。
- 它会调用 scripts\install.ps1。
- 默认安装目录由 GUI 选择，通常是 %LOCALAPPDATA%\闲鱼管理系统。
- 安装包不包含 Docker Desktop；目标电脑必须单独安装 Docker Desktop，并使用 Linux containers。
- 安装包内业务镜像和 MySQL/Redis 归档已经通过 SHA-256 验证。
- D:\本地安装包 当前的 .env 不存在是正常的，首次安装脚本会从 .env.example 创建。
- 安装器 EXE 当前未做 Authenticode 签名，Windows SmartScreen 可能显示警告。

当前包里的启动/修复脚本也已同步加入 --pull never，但如果重新构建安装包，必须从当前源树重新生成，不能只复制旧包里的脚本。

## 5. 已完成的验证

### 5.1 静态验证

以下检查已通过：

~~~powershell
# 解析当前源树所有 PowerShell
# 结果：85 个文件无语法错误

git -C C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite diff --check
# 结果：通过
~~~

### 5.2 商业交付验证

已通过：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite\tools\validate-commercial-delivery.ps1 -ProjectRoot C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite -PackageRoot D:\本地安装包
~~~

该验证包含 Docker Compose、工作流 YAML、PowerShell/BAT、安装包文件、归档数量、归档安全路径和 SHA-256。

### 5.3 Compose 离线配置

已确认离线变量解析出的镜像为：

~~~text
local/xianyu/xianyu-backend:1.2.0
local/xianyu/xianyu-websocket:1.2.0
local/xianyu/xianyu-scheduler:1.2.0
local/xianyu/xianyu-frontend:1.2.0
mysql:8.0
redis:7-alpine
~~~

### 5.4 真实 Docker 导入

此前已真实执行过完整 docker load 验证，6 个镜像均能导入，镜像 ID 和 layer 数量正确。当前机器还存在多个历史版本标签，调试时不要使用宽泛的 docker image prune 或全局清理命令。

### 5.5 失败保护验证

使用无效 Registry 地址模拟构建失败后确认：

- 旧 .tar.gz 数量和 SHA-256 未变化。
- 旧 offline-manifest.json 未变化。
- 临时 .offline-registry-work 已清理。

## 6. 增量更新与跨版本升级逻辑

当前在线更新器 deploy/update-xianyu-gui.ps1 的设计是直接从本机版本切换到远程 latest.json 声明的版本，不需要逐个安装中间版本：

1. 拉取并校验 latest manifest，必要时校验 RSA-SHA256 签名。
2. 比较本地 VERSION.txt、BUILD_ID.txt 与远端 version/build。
3. 对四个应用服务逐服务获取远程 manifest digest。
4. 若本机已有同 digest 镜像，跳过拉取。
5. 若上一版本标签和目标版本 digest 相同，使用本地 docker tag 复用镜像。
6. 只有 digest 不同或本地缺失时才 docker pull 对应服务。
7. 所有镜像成功后，使用 docker compose up -d --force-recreate --no-build --pull never。
8. 校验容器实际镜像引用、容器 image ID 和前端 HTTP 状态。
9. 失败时恢复旧 .env，并用 --pull never 尝试本地回滚。
10. 客户端维护包单独暂存，下一次启动由 apply-client-update.ps1 替换。

离线状态修复脚本会写入 app\runtime-offline-state.json，记录离线 bundle 的版本、image tag、image ID 和远程 digest；下一次远程更新前可用它判断本机当前镜像是否匹配。

注意：当前“离线安装包”并不意味着永远禁止应用更新器访问远程清单。安装/启动/离线修复阶段禁止镜像 pull；在线更新器在发现新版本时仍可能访问 latest.json 并切换到 remote 模式。这是后续需要产品确认的行为：

- 若离线电脑允许联网检查更新，保持现状并继续验收。
- 若离线电脑必须完全断网，应增加 offline-only 更新策略，禁止远程 manifest 检查，改为人工注入新版本 bundle。

## 7. 推荐下一步

1. 阅读并理解本文件、第 3 节未提交文件和对应 diff。
2. 对三个构建器做更细的故障注入测试，尤其是旧文件已移入 backup、新文件 Move-Item 失败的提交异常场景。
3. 在全新的临时输出目录构建离线安装包，不要覆盖 D:\本地安装包：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\build-windows-installer.ps1 -OutputDirectory D:\xianyu-package-test -Force -IncludeDockerImages
~~~

4. 对新包执行 validate-commercial-delivery.ps1，核对 offline-manifest.json、归档 SHA-256 和 docker compose config --images。
5. 若条件允许，在干净 Windows + Docker Desktop Linux containers 电脑上验收：首次安装、断网安装、重复点击安装、inject-offline-images.bat、启动、停止、诊断和失败恢复。
6. 验证多版本更新：旧版本 → 1.2.0、旧版本 → 更高版本；确认相同 digest 服务不会重复 pull，变化服务只更新对应镜像。
7. 决定是否实现完全离线更新模式，以及如何注入新版本 bundle。
8. 确认签名公钥和 release manifest 流程后，再考虑提交或发布；本轮默认不 push、不发布。

## 8. 常用安全检查命令

查看当前改动：

~~~powershell
git status --short --untracked-files=all
git diff --stat
git diff --check
~~~

重新检查 PowerShell 语法：

~~~powershell
$root = 'C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite'
$errors = @()
foreach ($file in Get-ChildItem $root -Recurse -File -Filter *.ps1) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    $errors += @($parseErrors)
}
$errors
~~~

查看离线 Compose 镜像解析：

~~~powershell
$env:XR_DEPLOY_MODE = 'offline'
$env:XR_IMAGE_REGISTRY = 'local'
$env:XR_IMAGE_NAMESPACE = 'xianyu'
$env:XR_IMAGE_TAG = '1.2.0'
docker compose --project-directory D:\本地安装包\app --env-file D:\本地安装包\app\.env.example -f D:\本地安装包\app\docker-compose.yml config --images
~~~

验证包内归档清单：

~~~powershell
$manifest = Get-Content -Raw D:\本地安装包\resources\images\offline-manifest.json | ConvertFrom-Json
foreach ($entry in $manifest.images) {
    $path = Join-Path D:\本地安装包\resources\images $entry.archive
    $actual = (Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne "$($entry.sha256)".ToLowerInvariant()) {
        throw "hash mismatch: $($entry.name)"
    }
}
~~~

## 9. 明确禁止事项

- 不要 git reset --hard。
- 不要 git checkout -- 恢复文件。
- 不要删除未跟踪的离线脚本、repair 脚本或 artifacts。
- 不要运行 docker system prune、全局 docker image prune 或删除所有 volumes。
- 不要把 D:\本地安装包 当作临时目录覆盖重建。
- 不要在没有备份的情况下运行 windows-reset-xianyu-docker.ps1 -AllDockerData -Force。
- 不要未经用户确认推送 Registry、更新 latest.json、发布新版本或提交线上部署。
- 不要只验证文件存在就认为离线镜像有效，必须验证 manifest、SHA-256、归档内容和真实导入后的 image ID。

## 10. 交接回传格式

下一位 Agent 完成工作后，请重新交接以下内容：

~~~text
1. 使用的工作树、分支或 commit：
2. 本次目标：
3. 实际修改的文件：
4. 保留的未提交/未跟踪文件：
5. 离线包输出目录和版本：
6. 每个归档是否通过 SHA-256：
7. 是否实际 docker load，结果是什么：
8. 是否验证 docker compose config --images：
9. 是否验证离线启动 --pull never：
10. 是否验证失败保护和旧包保留：
11. 是否验证跨版本增量更新：
12. 已知问题、阻塞项和需要用户决定的事项：
13. 是否提交、是否推送、是否发布：
~~~

交接文档本身也应随代码一起保留，后续 Agent 必须在本文件基础上追加更新，不要删除历史验证记录。

## 11. 2026-09-10 Docker/WSL 安装更新包

针对新电脑上旧版残留 Docker Desktop、winget 提示“已安装且无适用升级”后被误报为“Docker Desktop missing”，以及 WSL 中文输出乱码的问题，已追加以下修复：

- `tools/windows-installer-docker.ps1`
  - Docker Desktop 检测增加卸载注册表、InstallLocation、DisplayIcon、运行进程和用户目录候选路径。
  - winget 返回非 0 后重新检查 Docker Desktop；只要磁盘上找到有效程序，就直接启动并等待 Docker Engine，不再把“已安装无升级”当成未安装。
- `tools/windows-installer-wsl.ps1`
  - WSL 命令改为临时二进制文件捕获 stdout/stderr，再按 BOM、UTF-8、系统默认编码解码，避免 PowerShell 5.1 中文环境出现 `�` 乱码。
  - 保留真实退出码；乱码修复不会掩盖 WSL 命令本身的失败。
- 已同步修改当前安装包：
  - `D:\本地安装包\resources\docker-bootstrap.ps1`
  - `D:\本地安装包\resources\prepare-wsl.ps1`
- 可直接覆盖替换的更新包：
  - `artifacts/xianyu-installer-update-20260910-docker-wsl.zip`

更新包内保持安装包相对目录结构，解压后把 `resources\docker-bootstrap.ps1` 和 `resources\prepare-wsl.ps1` 覆盖到旧安装包的同名位置即可。它不包含 Docker 镜像、应用数据、数据库数据或用户配置，不会删除 Docker 镜像/卷。

本轮验证：四个脚本均通过 PowerShell AST 语法解析；源码文件与 D:\本地安装包中的对应文件 SHA-256 一致。尚未在真实新电脑上复现 winget/WSL 全流程，交付后应优先验证“旧 Docker Desktop + 保留镜像 + 重新安装”场景。

## 12. 2026-09-10 首次启动更新检查修复

用户反馈安装成功后首次打开软件，更新器直接执行 `update.ps1 -Headless`，在安装目录尚未生成 `.env` 时先报“缺少 docker-compose.yml 或 .env”，随后又执行 Docker 状态检查并产生第二个错误。该流程已修复：

- `deploy/check-xianyu-update.ps1`：缺少 `.env` 时记录 `check_skipped reason=environment_missing` 并以成功状态退出；更新检查是可选功能，不再阻断启动。
- `deploy/update-xianyu-gui.ps1`：保留 `docker-compose.yml` 缺失为真实错误；仅缺少 `.env` 时发送“跳过本次更新检查”结果并立即返回，不再调用 Docker Compose。
- 已同步到当前安装包：
  - `D:\本地安装包\app\deploy\check-xianyu-update.ps1`
  - `D:\本地安装包\app\deploy\update-xianyu-gui.ps1`
- 新的可覆盖更新包：
  - `artifacts/xianyu-installer-update-20260910-startup-check-fix.zip`

该更新包包含本节两个首次启动更新检查修复，以及上一节的 Docker Desktop/WSL 两个修复，共四个文件。已有有效 `.env` 的安装仍按原流程执行更新检查；本修复不会自动用 `.env.example` 覆盖或生成密码，避免破坏现有环境。

## 13. 2026-09-10 三界面重做与全状态验证（UI 轮）

按 `D:\Desktop\UI界面修改意见.txt` 完成安装向导、更新器、服务控制台三个窗口的重做与真机验证。本轮全部改动集中在 `tools/launcher/XianyuLauncher.cs`、`tools/launcher/XianyuLauncherCore.cs`（未提交）。

**协议与测试钩子**

- 事件协议 `@@XIANYU_UI@@{json}`（stage/meta/result/progress）与 `@@XIANYU_STATUS@@{json}` 保持不变；C# 只渲染，脚本决定真实状态。
- 新增环境变量测试钩子 `XIANYU_GUI_REPLAY`：指向文件则每次脚本运行回放该文件；指向目录则按文件名顺序第 N 次运行回放第 N 个文件（`1-*.txt`、`2-*.txt`…）。回放直接喂给 `ScriptRunner` 的 onLine 回调，支持 `#exit N`、`#delay MS` 指令，零副作用（不启动 powershell、不碰 Docker、不联网）。剧本在 `D:\xianyu-ui-build\replay\`。
- `XIANYU_LAUNCHER_TRACE=1` 写 `logs\launcher-trace.log`；本轮排障靠它定位了多个问题。均为测试专用开关，不影响生产路径。

**本轮修复清单（按根因）**

1. TableLayoutPanel 未声明 RowStyle 的行默认 AutoSize，Dock=Fill 子级按 100px 测量导致文字截断/重叠——全文件补 42 处显式行样式；StageRow 详情行 26px + AutoEllipsis。
2. `AppendLog` 在 stdout 异步读线程里直接操作 RichTextBox 抛异常杀死读线程（更新器收不到 result 事件的元凶）——改 BeginInvoke marshal + 处理器 try/catch。
3. 失败语义分三级：`failed`（红色横幅"更新失败，当前版本未受影响"+重试）、`rolled_back`（琥珀"已安全恢复到版本 vX"+回滚卡+重试）、`rollback_failed`（红色"自动恢复未完成"+诊断按钮+查看日志）。失败时不再自动展开日志。
4. `ShowBanner` 清理横幅时把 failureCard 一并移出容器——失败摘要卡从未显示过——保留条件修正；`ResizeBannerArea` 高度同步 140。
5. `Panel.BackColor` 默认 Transparent 导致 UserPaint 控件 `g.Clear` 出黑底（HeroBand、StatusBadge、IconBox、Ghost 按钮四处）——新增 `Ui.ResolveBackdrop` 沿父链取首个不透明色统一修复。
6. Lucide 图标数据 `square-arrow-out-up-right` 一个条目塞了三条路径（`|` 分隔），`GetShapes` 不拆分导致解析器在 `|` 处零长度线段死循环 → OutOfMemory → 按钮画成红叉——`GetShapes` 按 `|` 拆分 + 解析器加垃圾字符跳过与 20000 次迭代保险丝。
7. 服务控制台 `RefreshLayoutMode` 在构造早期 OnResize 时 cards 字典为空，KeyNotFound 中断且 `wide` 标志被污染，四张服务卡永远留在隐藏的 gridTwo——加 `cards.Count` 守卫与可见性一致性检查。
8. 折叠日志区 CollapsibleSection 构造未设 Height（默认 100 抢空间）；AutoScroll 容器内 Dock=Top 卡片被压缩——改无 Dock + 显式 Size + Resize 同步宽度。

**验证结果（真机截图 + UIA，均通过）**

- 安装向导 5 步：欢迎/环境检测（真实 Docker/WSL/离线包/网络四项）/安装位置（真实 95GB 可写校验）走真实脚本；正在安装（6 行进度、5/6 计数、日志条浅色）/完成页（服务摘要+访问地址+快捷方式勾选+启动按钮）走回放。
- 更新器 6 态：检查失败（真实 HTTPS 拒绝）、available、latest、completed（6/6）、rolled_back（琥珀+3/6）、rollback_failed（红+运行自动诊断）全部渲染正确。
- 服务控制台：四卡状态色（绿/绿/红/灰）、异常 summary、真实活动流、新版本链接齐全。
- 截图目录 `D:\xianyu-ui-build\shots\`（22-63 号为本轮），验证工具：PrintWindow 离屏截图（不受遮挡）+ UIA BoundingRectangle + PostMessage 无鼠标点击（避免与用户抢指针）。

**尚未验证**：~~200%/150% DPI 缩放~~（已于 §14 四档通过）；真实更新服务器的 available→下载→回滚全链路（本轮只测了 UI 层）；`restart_client` 结果态；升级安装（覆盖旧版本）场景。下一轮建议用 `build-windows-installer.ps1` 出正式包后在虚拟机做一次端到端。

**边界确认**：本轮所有测试均未触碰 `D:\本地安装包`，未执行任何真实安装/镜像拉取/容器启停（用户调试容器不受影响），未运行 docker 写操作。

## 14. 2026-09-11 控制台微调、DPI 四档、品牌图标统一（UI 第二轮）

改动集中在 `tools/launcher/XianyuLauncher.cs`、`tools/launcher/XianyuLauncherCore.cs`、`tools/build-launcher.ps1`、新增 `assets/xianyu-app-icon.png`（均未提交）。

### 14.1 控制台/窗口微调（用户反馈驱动，全部截图验证）

1. 仪表盘头部行高 96→108（StatusBadge 被卡片遮挡的根因：24px 行 + Label 默认 9px 外边距把内容下移溢出）；headText 行 42/26/26，两个 Label 与 systemStatus 全部 `Margin = new Padding(0)`。
2. 启动期徽标覆盖条件收窄：仅 `level == "unknown" || level == "down"` 时显示"正在启动服务并检查更新…"，否则如实显示脚本状态。
3. 页脚列序改 Percent(100)+AutoSize（原 AutoSize+Percent 导致版本串"v1.2.0 ·"被截断）。
4. "查看全部"按钮宽 116、活动区头部行 44/padding(16,8,12,0)（原 30px 行减 12px padding 放不下 28px 按钮，四字顶部被裁）。
5. 按用户要求**移除**最近活动行的悬停 tooltip（rowRects/MouseMove/OnMove 全部删除，PaintRecords 保留行命中绘制）——注意这与规格 §22 相悖，是用户当面拍板的偏离。
6. "诊断与日志"从"静默后台脚本+notepad"改为打开任务窗口：`AppOps.LaunchPackageExecutable(packageRoot, "xianyu-diagnostics", LauncherText.Diagnostics, "--diagnostics", "", "")`，找不到诊断程序时徽标提示"找不到诊断程序，请重新安装"。
7. TaskWindow 两个渲染 bug：StatusBadge 未设 `Dock = DockStyle.Fill`（AutoSize 列内宽 0 不可见）；"完成"按钮文字裁切（Panel+Anchor+手动 Left 布局失效）→ 改 `FlowLayoutPanel { Dock=Fill, FlowDirection=RightToLeft, WrapContents=false }` 装 120×40 按钮（与更新器页脚 1432-1449 行同款模式，改按钮布局优先用这个模式）。

### 14.2 DPI 四档验证（补上 §13"尚未验证"的头号缺口）

- **注册表方案在 Win11 无效**：HKCU `Control Panel\Desktop` 的 `LogPixels`+`Win8DpiScaling`+广播 `WM_SETTINGCHANGE('User32DpiHint')` 后新进程仍是 96dpi（真实缩放存于 per-monitor 二进制 blob）。已恢复注册表原值（LogPixels 删除、Win8DpiScaling=0）。不要再走这条路。
- **新增测试钩子 `XIANYU_FORCE_DPI`**（接受 `1.5` 或 `150`）：`Ui.DpiScale` 取强制值，新增 `Ui.ActualDpiScale` 记录真实 DC 比例，`Ui.Pt()` 乘 `DpiScale/ActualDpiScale` 补偿点字体 → 布局与文字同步放大。`LauncherWindow` 的 `AutoScaleMode.None`（core ~1140 行）保证 WinForms 不会二次缩放。该钩子与真实 DPI 走同一代码路径，环境变量门控，生产无影响。
- **修复真实 bug**：`LauncherWindow` 尺寸钳制原用 `Screen.FromPoint(Cursor.Position).WorkingArea`——窗口大小取决于鼠标当时停在哪个显示器，多屏环境高 DPI 下必然出错（实测副屏竖屏时 1280 设计宽被钳到 1390/1360 物理 px）。已改 `Screen.PrimaryScreen.WorkingArea`（窗口本就 CenterScreen）。
- **结论**：控制台 125/150/175/200 + 安装向导 200 + 更新器 200 全部通过（`D:\xianyu-ui-build\shots\dpi-*.png`）。1440p 屏 @200% 窗口被钳到 2480×1320，内容无裁切（只压缩卡片留白）；4K @200% 无钳制。
- **残留**：仿真未覆盖 DWM 圆角与光标位图，建议在 VM 真 4K@200% 抽查一次截图即可销项。

### 14.3 品牌图标统一（金鱼）

- **正确图标 = 仓库 `assets/xianyu-launcher.ico`（金鱼，16-256 六帧）**。它一直通过 build 脚本 `/win32icon` 内嵌进 exe，任务栏/Alt-Tab/资源管理器从未出错。"图标没换"的实际问题是**窗口内**标题栏与品牌栏画的是手绘占位蓝底鱼（`Lucide.DrawLogoMark`）。
- **严禁误用**：`D:\Desktop\online-1.0.6\assets\` 下的放大镜+五平台花瓣图标是**采集平台**的图标，不是本产品的。本轮曾误将其做成 ico/资源并部署，已全部回滚（git checkout 恢复 ico，删除误放 png，重编译确认 exe 无残留）。
- **GDI+ 陷阱**：`new Icon(path,w,h)`/`Icon.ToBitmap` 对 PNG 压缩帧的 ico 会**误读成噪点**——不代表 ico 损坏。要看真实帧内容：手动解析 ICO 目录（6 字节头+16 字节/帧），按 offset/size 切出原始 PNG blob 直接落盘。
- **新构建依赖**：`assets/xianyu-app-icon.png`（金鱼 256 帧裁掉透明边距后 232×195，64KB）。build 脚本以 `/resource:...,xianyu-app-icon.png` 嵌入。**此文件必须入库**；缺失时 `Ui.LoadBrandLogo()` 得 null，各绘制点回退手绘占位鱼（不会崩，但图标不对）。
- **绘制机制**：`Ui.LoadBrandLogo()`（Program.Main 启动时调用）+ `Ui.DrawLogo(g, box[, alpha])`——等比居中、透明直贴、HighQualityBicubic，alpha<1 时走 ColorMatrix（注意 `DrawImage` 带 ImageAttributes 的重载**只接受 int Rectangle**，RectangleF 版不存在，CS1502）。
- **接入点与尺寸**（用户要求：任何位置不得垫背景色块）：标题栏 LogoBox 24→32（列 24→36，三窗口共用）；安装向导品牌栏 RailPanel 占位 44→真图 64（y 88→80）；更新器 hero 标题列 34→44（IconBox `__logo` 走全框等比）；HeroBand 右下装饰水印 100px @10% alpha。死代码 `productIcon`/`TryLoadIcon` 文件读取已删。
- 排障教训：上一轮"没换成功"是水印处 DrawImage 重载编译失败 → pkg 里静默部署了旧 exe。**build 后必须先 grep error CS 再 Copy-Item 部署**。

### 14.4 当前工作区状态（本轮末）

- HEAD：f322ecc（§0/§1 里的 57da30c 已过时）。
- 已修改未提交：`deploy/update-xianyu-gui.ps1`、`docs/AGENT_HANDOFF_20260910_OFFLINE_INSTALLER.md`、`tools/build-launcher.ps1`、`tools/build-windows-installer.ps1`、`tools/import-offline-image-bundle.ps1`、`tools/launcher/XianyuLauncher.cs`、`tools/launcher/XianyuLauncherCore.cs`、`tools/windows-installer-install.ps1`、`tools/windows-installer-update.ps1`。
- 新增未跟踪（**要入库**）：`assets/xianyu-app-icon.png`。
- 无关未跟踪（勿混入提交）：`.tmp-lucide/`、`artifacts/pelican-frame-*.png`、`pelican-bicycle.html`。
- 提交/推送仍需用户确认。
- 冒烟构建：`& $env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\build-launcher.ps1 -OutputDirectory 'D:\xianyu-ui-build\launcher'` → 杀 `*闲鱼*` 进程 → `Copy-Item 'D:\xianyu-ui-build\launcher\*' 'D:\xianyu-ui-build\pkg\' -Force`（pkg 内 10 个 exe 同源改名）。

### 14.5 下一位 Agent 的待办（按优先级）

1. **VM 端到端**：`build-windows-installer.ps1` 出正式包，跑规格 §25 验收矩阵未测的 11 项（Docker 未装引导、WSL 3010、离线无网、签名失败、升级安装、`restart_client` 结果态等）+ 真 4K@200% DPI 抽查截图。
2. **真实更新服务器**全链路：available→下载→应用→健康检查→（故障注入）回滚。
3. `status.ps1` 真实 docker 分支验证（本轮只验证了 UI 消费侧，脚本侧依赖用户调试容器，不可动）。
4. 等用户拍板两问：trace 开关（`XIANYU_LAUNCHER_TRACE`）交付包是否保留；路径页"浏览"按钮焦点框样式。
5. 已知 Windows 行为：exe 原地替换后资源管理器图标缓存可能不刷新，属系统缓存非 bug（`ie4uinit.exe -show` 可刷，勿主动执行）。

**边界确认（本轮）**：未调用任何 docker 命令；未触碰 `D:\本地安装包`；注册表 DPI 试验已完全恢复原值；用户调试容器全程未受影响。
