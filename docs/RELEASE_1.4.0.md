# 闲鱼管理系统 1.4.0 发布说明

- 版本：`1.4.0`
- 构建 ID（本地标记）：`20260911-1.4.0-status-separation`
- 基线：`v1.3.8`
- 前置批次：`docs/RELEASE_1.3.8.md`（IM 保活 + 设备指纹 + 风控标记清理）

1.4.0 修复 1.3.8 诊断中列出但**尚未处理**的 4 类遗留缺陷，核心是把
「登录态失效」与「IM 链路故障」彻底分开，并让自愈路径重新可达。

> **版本号说明**：本次按**次版本号**发布（1.3.8 → 1.4.0）而非修订号。
> 改动包含账号状态语义调整（新增 `im_status`，与登录态 `status` 分离）、
> 数据库新增 5 列、以及任务状态语义修正，行为变更幅度超出修订号范围。
> 升级兼容：客户端可无感升级，后端启动时自动补列，无需手工迁移。

## 1. 修复的缺陷

### 1.1 P0：IM 失败被写成登录态失效，把局部故障放大成整账号停摆

**问题**：`websocket/app/xianyu_runtime.py` 的续期验收分支里，Passport 与浏览器
已经**续期成功**（即登录态确认有效），只是随后取 IM Token 失败，代码却执行：

```python
account.status = "expired"          # ← 错误：登录态其实有效
account.cookie_expire_at = now
```

后果：账号被标记为登录态过期，而**网页侧同步任务按 `status` 过滤**，
于是整个账号从商品/订单同步里消失。用户看到的现象就是
「网页功能也一起挂了」，而实际坏的只是聊天链路。

**修复**：IM Token 失败只写新字段 `im_status = "error"`，**不再触碰 `status`**：

```python
account.im_status = "error"         # 只记录 IM 链路故障
runtime.update("im_error", "登录态有效，但获取 IM Token 失败：…")
```

只有平台确认登录态失效时，才由新增的 `_mark_login_expired()` 写
`status = "expired"`（并同时置 `im_status = "expired"`）。
这是 `status` 在运行时的**唯一**写入点，两类故障从此不可能再混淆。

### 1.2 P0：`expired` 账号的自愈路径永久不可达

**问题**：`scheduler/app/jobs/account_sync.py` 只选 `status == "active"`：

```python
statement = select(Account).where(Account.status == "active", ...)
```

而 `sync_account_products()` 内部恰好带**唯一一条自动恢复链路**
（检测会话过期 → 强制续期 → 重试同步）。账号一旦被标记 `expired`，
这条链路就再也执行不到 —— 自愈能力被自己的过滤器关掉了。

**修复**：

```python
Account.status.in_(["active", "expired"])
```

同时新增**续期冷却窗口**，避免新增的 `expired` 覆盖把 Passport 打成高频请求：

- `RENEWAL_ATTEMPT_COOLDOWN_MINUTES = 15`：同一账号两次自动续期的最小间隔；
- 冷却只对后台来源生效；`manual`（人工点击）与 `runtime`（自带 5 分钟冷却）
  在 `COOLDOWN_EXEMPT_SOURCES` 中，**始终放行**，避免"点了续期却没反应"；
- 每次尝试（不论成败）写入 `last_renewal_attempt_at`，冷却才有依据。

### 1.3 静默成功：处理 0 个账号却报 `completed`

**问题**：三处任务的判定都是
`"partial" if failed_count else "completed"`。当账号数为 0 时
`failed_count == 0`，于是报 `completed` —— 界面上是绿色成功，**实际什么都没做**。

实证：1.3.8 诊断时观察到 `sync_products` 在账号已过期（09:17）之后
仍于 09:20 报 `completed`，而它处理的账号数是 0。

**修复**：三处统一改为「无账号 → `skipped`」并给出明确说明：

- `scheduler/app/jobs/account_sync.py`（`sync_products` / `sync_orders`）
- `scheduler/app/jobs/account_renewal.py`（`refresh_cookies`）
- `scheduler/app/jobs/account_renewal.py`（`refresh_tokens`）

调度器已把 `skipped` 视为正常结束（`scheduler/main.py`），不会刷错误。

### 1.4 验证类失败被误判为「需重新扫码」

**问题**：滑块、人脸、Baxia punish（`_____tmd_____` / `punish`）
与「登录态失效」共用同一条提示"请重新扫码登录"。这会误导用户（明明只需过验证）
和排查方（误以为登录态坏了）。

**修复**：新增 `VERIFICATION_MARKERS` 与 `describe_failure()`，把失败原因
翻译成正确的下一步动作：

| 失败类型 | 提示 |
| --- | --- |
| punish / 滑块 / 人脸 | 闲鱼要求完成安全验证（滑块/人脸），请先在浏览器或客户端完成验证后再试 |
| `FAIL_SYS_USER_VALIDATE` 等设备校验 | 闲鱼要求完成设备安全验证，请重新扫码登录或配置远程 Token |
| `FAIL_SYS_SESSION_EXPIRED` | 登录态已失效，请重新扫码登录 |

`renew_account_session()` 的返回值新增 `verification_required` 布尔字段，
供上游区分处理。

### 1.5 死字段清理：三个「看着像真的、其实是假的」字段

| 字段 | 此前状态 | 现在 |
| --- | --- | --- |
| `cookie_last_renewed_at` | 代码零引用，DB 值停留在旧版本 | 续期成功时写入真实时间 |
| `cookie_next_renewal_at` | 代码零引用，显示 `2026-09-17` 但与实际调度（cron `*/20`）不符 | 续期成功时按调度约定写入 |
| `last_renewal_attempt_at` | 不存在 | **新增**，冷却窗口的唯一依据 |

三个字段均已加入 `Account` 模型与旧库补列迁移，并在账号接口中返回，
排查时看到的时间现在与真实调度一致。

### 1.6 状态区分可见化

- `Account` 模型新增 `im_status`（`unknown` / `connected` / `expired` / `error`）；
- 账号接口 `serialize()` 返回 `im_status`、`im_device_id` 与三个续期时间字段；
- 账号详情弹窗新增「聊天链路」一行：
  - 正常 / **IM Token 获取失败（登录态仍有效）** / 连接已失效 / 未知
- 前端 `RewriteAccountContentDetail.account` 类型同步补齐。

## 2. 数据库迁移

`common/db/session.py` 的 `_migrate_account_columns()` 现在补齐：

```sql
ALTER TABLE xr_accounts ADD COLUMN im_device_id VARCHAR(128) NULL;
ALTER TABLE xr_accounts ADD COLUMN im_status VARCHAR(16) NOT NULL DEFAULT 'unknown';
ALTER TABLE xr_accounts ADD COLUMN last_renewal_attempt_at DATETIME NULL;
ALTER TABLE xr_accounts ADD COLUMN cookie_last_renewed_at DATETIME NULL;
ALTER TABLE xr_accounts ADD COLUMN cookie_next_renewal_at DATETIME NULL;
```

**已在真实 MySQL 8.0 上验证**（独立临时库 `xr_migrate_check`，不触碰业务库）：

- 迁移前字段：`id,user_id,account_name,goofish_id,cookie,proxy,status,cookie_expire_at,created_at,updated_at`
- 迁移后 5 个新列全部就位，`im_status` 为 `varchar(16) NOT NULL DEFAULT 'unknown'`
- **旧数据完整保留**（`legacy-account` / `status=active` / `im_status=unknown`）
- 迁移函数检查已有列，**重复启动不会重复 ALTER**（测试锁定幂等性）

## 3. 验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| Python 语法 | `compileall backend common websocket scheduler deploy` | 通过 |
| 后端测试 | `unittest discover -s backend/tests -t .` | **164 passed** |
| 前端类型检查 | `tsc --noEmit` | 通过 |
| 前端 Lint | `eslint --max-warnings 0` | 通过 |
| 前端构建 | `vite build` | 通过 |
| compose 校验 | `docker compose config --quiet` | 通过 |
| 迁移 DDL | 真实 MySQL 8.0 临时库实测 | 通过（含数据保留） |

新增 `backend/tests/test_account_status_and_renewal_gate.py`（37 个用例）：

- **死锁防护**：同步任务源码必须包含 `in_(["active", "expired"])`，
  且不得再出现 `Account.status == "active"`；并校验
  `sync_account_products` 确实带自愈逻辑（前提校验）。
- **冷却窗口**：无尝试→不冷却；近期尝试→冷却；超期→放行；
  `manual` / `runtime` 豁免；非强制续期不受限；带时区时间戳不抛异常。
- **状态分离**：IM Token 失败分支**不得**出现 `account.status = "expired"`；
  提示必须说明「登录态有效」；成功后写 `im_status = "connected"`；
  `serialize()` 暴露 `im_status` 且旧对象缺列时不崩。
- **`_mark_login_expired` / `_persist_im_status`**：前者同时写两个状态，
  后者**不得触碰** `status`。
- **静默成功**：三个任务在 0 账号时都返回 `skipped`；
  调度器把 `skipped` 视为非失败。
- **验证分类**：punish/滑块/人脸识别为验证类；普通过期识别为需扫码；
  验证类提示**不含"扫码"**二字。
- **迁移**：旧库补全 5 列；`im_status` 为 `NOT NULL DEFAULT 'unknown'`；
  幂等（已存在则一条 ALTER 都不发）；表不存在时跳过。

## 4. 关于 1.3.8（重要状态说明）

`v1.3.8` 标签已推送到 GitHub，但其流水线 **失败**：

```
drone-scp error: dial tcp ***:22: i/o timeout
```

失败发生在 `manifest` 作业的「Publish latest manifest last」步骤，
全部镜像构建作业（backend / websocket / scheduler / frontend）均为 `success`。

**线上未受任何影响**：

- `latest.json` 仍为 `1.3.7`（`build_id=de918c256607-1.3.7`）；
- 1.3.8 镜像虽已推入 Registry（`xianyu-backend:1.3.8` 等返回 200），
  但**从未被清单引用**，客户端不会拉取；
- 失败原因是 GitHub Runner 无法通过 22 端口 SSH 到发布服务器，属**基础设施/网络问题**，
  与代码改动无关（1.3.5、1.3.7 同一步骤曾成功，疑为瞬时超时）。

**发布前需要确认 SSH 连通性**，否则清单上传会再次失败。

## 5. 验收清单（待发布后人工执行）

| # | 验收项 | 期望结果 |
| --- | --- | --- |
| 1 | 后端启动后检查字段 | `xr_accounts` 含 5 个新列，`im_status` 默认 `unknown` |
| 2 | 只有 IM 故障的账号 | 账号详情「聊天链路」显示 **IM Token 获取失败（登录态仍有效）**，且登录态仍为 active |
| 3 | 上述账号是否仍参与商品同步 | 是（不再被 `status` 过滤跳过） |
| 4 | 账号详情「实时连接」 | IM 故障时显示 `IM 未连接`，而非笼统「离线」 |
| 5 | 手动点击续期 | 即使处于冷却窗口内也应立即执行（`manual` 豁免） |
| 6 | 连续触发续期 | 15 分钟内只实际尝试一次（看 `last_renewal_attempt_at`） |
| 7 | 无可用账号时的任务状态 | 显示 `skipped` +「没有可处理的账号」，**不再显示 completed** |
| 8 | 撞到滑块/人脸时 | 提示「请先完成安全验证」，**不再提示重新扫码** |
| 9 | `cookie_last_renewed_at` | 续期成功后为真实时间，不再是旧版本遗留值 |
| 10 | 长连接恢复后 | `im_status` 变为 `connected` |

历史回归项（IM 保活、设备指纹、风控标记、套餐权限、注册审批）见
`docs/RELEASE_1.3.8.md` 与 `docs/RELEASE_1.3.7.md`。

## 6. 涉及文件

- `websocket/app/xianyu_runtime.py`：`_mark_login_expired`、
  `_persist_im_status`、IM 失败分支改写、`im_status` 写入
- `common/services/account_renewal.py`：冷却窗口、时间戳维护、
  `verification_required`、`describe_failure` 接入
- `common/services/cookie_renewal.py`：`VERIFICATION_MARKERS`、
  `requires_verification_message`、`describe_failure`
- `common/models/accounts.py`：`im_status`、`last_renewal_attempt_at`、
  `cookie_last_renewed_at`、`cookie_next_renewal_at`
- `common/db/session.py`：`_migrate_account_columns` 扩展（5 列，幂等）
- `scheduler/app/jobs/account_sync.py`：包含 `expired`、0 账号报 `skipped`
- `scheduler/app/jobs/account_renewal.py`：0 账号报 `skipped`（两处）
- `backend/app/api/routes/accounts.py`：`serialize` 暴露新字段
- `frontend/src/pages/accounts/Accounts.tsx`：`im_error` 状态、聊天链路展示
- `frontend/src/api/accounts.ts`：类型补齐
- `backend/tests/test_account_status_and_renewal_gate.py`（新增 37 个用例）

版本文件：`VERSION.txt`、`BUILD_ID.txt`、`frontend/package.json`、`frontend/package-lock.json`

## 7. 发布

```powershell
npm ci --prefix frontend
$env:APP_VERSION='1.4.0'
npm run build --prefix frontend
python -m compileall -q backend common websocket scheduler deploy
python -m unittest discover -s backend/tests -t .
```

发布顺序（交接文档 §7）：先上传客户端包与签名，最后上传 `latest.json`。
**发布前先确认发布服务器 22 端口从 GitHub Runner 可达**（1.3.8 就失败在这里）。
