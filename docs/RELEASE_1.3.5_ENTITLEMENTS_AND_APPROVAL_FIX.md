# 套餐权限独立页面与注册审批修复实施说明

- 基线版本：`1.3.5`
- 基线工作树：`C:\Users\StarLink\.codex\worktrees\8172\xianyu-rewrite`
- 基线分支：`codex/release-1.3.3-client-handoff-fix`
- 基线提交：`152c751 docs: add Xianyu 1.3.5 agent handoff`
- 本文档对应交接文档 `docs/AGENT_HANDOFF_20260911_1.3.5.md` 第 9 节未完成项

本批次完成了交接文档 9.1（VIP/套餐权限独立界面），并修复了实施过程中暴露的 3 个线上功能性缺陷。
版本号未变更，仍是 `1.3.5`；发布时需重新构建后端镜像与客户端包。

## 1. 修复的缺陷（均为线上可复现）

### 1.1 P0：套餐权限接口从未注册，页面必然取不到数据

`backend/app/api/routes/admin_entitlements.py` 共 264 行、7 个接口，但**从未在 `backend/main.py` 中
`include_router`**。所有 `/api/v1/admin/entitlements/*` 请求都落到 `legacy_surface` 的兜底路由
`/api/v1/{full_path:path}`，被当作 `FeatureRecord` 记录处理。

表现：`/admin/entitlements` 页面永远显示“正在加载套餐配置…”或空白，读写套餐均无效。

修复：在 `backend/main.py` 注册该路由，且必须放在 `/api/v1` 兜底路由**之前**。

### 1.2 P0：注册申请待审批在管理员侧被错误显示为“正常”

链路本身是通的，问题在管理员页面显示：

- 云端 `authenticate()` 会正确拒绝 `status == 'pending'` 的账号（用户端提示“账号正在等待管理员审批”）——**用户看到的是真实状态**。
- 云端 `list_users` 返回 `status: 'pending'`，后端 `_serialize_remote_user` 也正确映射为 `PENDING`。
- 但前端 `Users.tsx` 的 `statusLabelMap` **没有 `PENDING` 键**，`statusLabelMap[user.status] || '正常'`
  回落到“正常”，徽标颜色也回落到绿色 ACTIVE。

同时，后端 `/admin/users/{id}/approve`、`/reject` 接口一直存在，但**前端从未调用**，
页面上没有任何审批入口，管理员无法通过界面批准注册申请。

修复：补 `PENDING` 状态映射（蓝色“待审批”徽标）、新增审批 API 客户端与「通过 / 拒绝」按钮、
表头上方显示待审批数量提示、编辑弹窗状态下拉补「待审批」选项。

> 注意：本修复只让状态正确显示，**不会自动批准历史待审批账号**，需管理员手动点一次「通过」。

### 1.3 高：调整 VIP 套餐报“请在统一认证服务中修改”

`frontend/src/api/admin.ts` 的 `mapAdminUser` 没有映射 `cloud_mode`（也没映射 `plan_code` /
`plan_expires_at`），导致 `user.cloud_mode` 恒为 `undefined`。于是 `UserFormModal` 判定
`isCloudEdit = false`，走本地账号分支提交了完整资料（`username`、`role`、`status` 等），
而后端云端模式只接受 `{plan, plan_code, plan_expires_at, expire_at}`，收到多余字段直接返回 409。

修复：补齐 `mapAdminUser` 字段映射；云端账号的编辑弹窗改为只读（隐藏保存按钮）并明确提示。

### 1.4 高：单用户功能授权在本机不生效

本机 `get_effective_entitlement` 只读本机 `user_entitlement_overrides` 表，而云端
`set_user_feature` 只写云端的 `entitlements_json`。两者从不同步，因此管理员为单个用户
开关的功能在本机业务拦截逻辑里不生效。

修复：登录时新增 `_sync_cloud_overrides()`，把云端功能授权镜像到本机覆盖表。

安全约束（已被测试锁定）：**只有云端明确返回了 `entitlements` 字典时才执行清理**。
旧版鉴权服务不返回该字段，此时必须保持本机授权不变，否则每次登录都会把用户授权清空。
镜像过程不修改 `auth_version`，避免登录时作废已签发的令牌。

### 1.5 中：个人设置看不到套餐详情

`/api/v1/users/me` 原本只返回 `expire_at`，从不返回套餐字段，因此“我的设置”无法展示 VIP 权益。

修复：接口补齐 `plan_code` / `plan_expires_at` / `entitlements`；个人设置新增只读套餐详情区块，
到期时间优先取 `plan_expires_at`（云端 VIP 以此为准），缺失时回落 `expire_at`。

## 2. 9.1 VIP/套餐权限独立界面（本轮目标）

`/admin/entitlements` 现在是**独立**的套餐权限中心，分两个页签：

- **套餐配置**：按套餐维护功能的启用与配额（`NORMAL` / `VIP` 及自定义套餐），
  并支持新增套餐（可从现有套餐复制功能授权，避免新套餐没有任何可授权功能）。
- **用户授权**：用户列表 + 权限配置面板。在此完成 **VIP 开通、套餐变更、套餐到期时间、
  单用户功能授权**，并支持「跟随套餐」删除单用户覆盖、回落到套餐默认值。

用户管理中套餐编辑入口的处理：

- `UserFormModal.tsx` 的**套餐下拉与到期日输入已删除**，改为只读展示 + 「去套餐权限开通」跳转按钮。
- `Users.tsx` 的“到期日”列改为只读“套餐”列（套餐编码 + 到期时间），并提供跳转到
  `/admin/entitlements?tab=users&user_id=<id>` 的「套餐权限」按钮，自动定位到该用户。
- 用户管理的职责收敛为：账号资料、角色、状态、密码、审批、启停、余额。

原套餐权限页里“给具体用户开通 VIP，请进入用户管理编辑该用户”的跳转提示已删除。

## 3. 可自动化验证（本轮已全部执行通过）

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| 前端类型检查 | `node node_modules/typescript/bin/tsc --noEmit`（frontend 目录） | 通过，0 错误 |
| 前端生产构建 | `$env:APP_VERSION='1.3.5'; node node_modules/vite/bin/vite.js build` | 通过，版本 1.3.5 |
| Python 语法 | `python -m compileall -q backend common deploy` | 通过 |
| 后端测试 | `python -m unittest discover -s backend/tests -t .` | 59 passed |

新增测试文件 `backend/tests/test_admin_entitlements.py`，覆盖：

- **路由注册防回归**：断言 7 条 `/api/v1/admin/entitlements/*` 路径已注册，
  且 `/api/v1/admin/entitlements/plans` 在兜底路由 `/api/v1/{full_path:path}` **之前**
  （这条断言专门锁死 1.1 那个 P0）。
- 功能键白名单校验、负配额/非布尔值拒绝、`reason` 去空格。
- 云端覆盖规范化：未知功能键忽略、非字典负载忽略、非法值丢弃、`expires_at` 正确解析。
- 云端覆盖镜像：写入、就地更新、清理本机陈旧受管键、**保留非受管历史键**、
  **云端未返回 entitlements 时不清空本机授权**、不改动 `auth_version`。

## 4. 本轮修复过程中被测试抓出的 2 个真实缺陷

1. `_normalize_override` 的 `reason` 未去空格。
2. `_sync_cloud_overrides` 在云端未返回 `entitlements` 时会清空本机授权（每次登录都会丢授权）。

两者均已修复并被测试覆盖。

## 5. 未自动化的部分与部署前置条件

下述内容无法在本机自动化验证，必须在发布后人工确认：

1. **后端镜像需重新构建发布**。1.1/1.3/1.4/1.5 的修复都在后端，仅更新客户端包不生效。
2. **云端鉴权服务需确认已部署 `deploy/cloud_auth/`**。本轮未改动该目录；
   但按交接文档 §5.2，如果线上鉴权服务不由主流水线自动部署，其账号会话阻断（HTTP 410）
   与 `entitlements` 字段返回都需要该目录的服务代码单独上线。
3. 历史待审批账号需管理员在用户管理页手动点「通过」。

## 6. 9.2 测试机验收清单（待人工执行）

建议使用一台 1.2.0 测试机和一台 1.3.4 测试机分别验证。

### 6.1 自动更新与运行时同步（沿用交接文档 §6 验收口径）

| # | 验收项 | 期望结果 |
| --- | --- | --- |
| 1 | 检测到新版本后是否自动开始更新 | 无需手动点“立即更新”，自动进入下载与安装 |
| 2 | 是否自动重启启动器 | 更新器发送 `restart_client`，新版启动器自动启动 |
| 3 | 是否最终同步到 1.3.5 Docker 镜像 | 见下方命令输出四个容器均为 1.3.5 |
| 4 | 是否不再卡在“回滚保护：检测中” | 检查阶段徽标不再长期停留检测中 |
| 5 | 是否不再卡在 2/6 | 进度正常推进至完成 |
| 6 | 是否保留系统登录状态 | 更新后无需重新登录 |
| 7 | 更新完成后进度条是否正常退出 | 不再持续滚动、有完成提示 |

验收命令（不能只看界面版本）：

```powershell
Get-Content D:\xianyu\app\VERSION.txt
Get-Content D:\xianyu\app\BUILD_ID.txt
Get-Content D:\xianyu\app\.env | Select-String 'XR_DEPLOY_MODE|XR_IMAGE_REGISTRY|XR_IMAGE_NAMESPACE|XR_IMAGE_TAG'
docker compose --project-directory D:\xianyu\app --env-file D:\xianyu\app\.env -f D:\xianyu\app\docker-compose.yml ps
```

### 6.2 本轮新增/修复功能的验收清单

| # | 验收项 | 期望结果 |
| --- | --- | --- |
| 8 | 管理员打开 `/admin/entitlements` | 能看到 NORMAL / VIP 套餐与功能矩阵（**修复前为空白**） |
| 9 | 修改套餐某功能配额并保存 | 提示保存成功，刷新后数值不丢失 |
| 10 | 新增一个自定义套餐并选择“从 VIP 复制” | 新套餐带出 VIP 的功能授权，而非全空 |
| 11 | 用户注册申请后，管理员查看用户管理 | 该账号显示蓝色**「待审批」**，不再显示“正常” |
| 12 | 管理员点「通过」 | 提示申请已通过；**用户端随后可正常登录** |
| 13 | 管理员点「拒绝」 | 用户端登录提示账号被拒绝 |
| 14 | 对某用户开通 VIP 并设置到期时间 | 保存成功，用户管理套餐列为 VIP，无 409 报错（**修复前报“请在统一认证服务中修改”**） |
| 15 | 为某用户单独开启“内置AI自动回复” | 该用户账号页内置 AI 可用（验证 1.4 镜像生效） |
| 16 | 点某功能的「跟随套餐」并保存 | 覆盖被删除，该功能回落套餐默认值 |
| 17 | 云端账号点「编辑」 | 弹窗只读，无保存按钮，并提示由云端服务管理 |
| 18 | 用户端打开「我的设置」 | 显示当前套餐编码、套餐到期日与各功能开通/剩余配额 |
| 19 | VIP 用户开启内置 AI 自动回复 | 关键词列表出现 336 条预置规则（1.3.4 回归项） |
| 20 | 管理员账号查看自身「我的设置」 | 不受套餐限制，功能显示为不限量 |
| 21 | 云端账号登录后重启客户端再看权限 | 单用户授权保持生效，未被登录流程清空（验证 1.4 安全约束） |

## 7. 涉及文件

后端：

- `backend/main.py`（注册套餐权限路由，置于兜底路由之前）
- `backend/app/api/routes/admin_entitlements.py`（功能键白名单校验、`copy_from` 建套餐、云端覆盖形态归一）
- `backend/app/api/routes/admin_users.py`（云端模式提示文案）
- `backend/app/api/routes/auth.py`（`normalize_cloud_overrides` / `_sync_cloud_overrides`）
- `backend/app/api/routes/user_profile.py`（`/me` 返回套餐字段）
- `backend/tests/test_admin_entitlements.py`（新增）
- `backend/tests/test_cloud_auth_login.py`（测试替身适配新增查询）

前端：

- `frontend/src/pages/admin/Entitlements.tsx`（重写为独立套餐权限中心）
- `frontend/src/pages/admin/Users.tsx`（审批入口、送审状态显示、套餐列只读、跳转权限中心）
- `frontend/src/pages/admin/UserFormModal.tsx`（删除套餐编辑，改只读提示）
- `frontend/src/api/entitlements.ts`（补齐用户授权接口）
- `frontend/src/api/admin.ts`（`cloud_mode`/套餐字段映射、审批接口）
- `frontend/src/api/settings.ts`、`frontend/src/pages/personalSettings/PersonalSettings.tsx`（个人套餐详情）

## 8. 发布前执行

```powershell
git fetch origin --tags
git checkout -b codex/entitlements-approval-fix v1.3.5
npm ci --prefix frontend
$env:APP_VERSION='1.3.5'
npm run build --prefix frontend
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-launcher.ps1 -OutputDirectory C:\Temp\xianyu-launcher-check -Force
python -m compileall -q backend common deploy
python -m unittest discover -s backend/tests -t .
```

发布顺序（沿用交接文档 §7）：先上传客户端包与签名，最后上传 `latest.json`。
发布清单必须使用 RSA-SHA256 签名。

不要使用 `git reset --hard`、`git clean -fd` 或其他会删除未跟踪文件的命令。
