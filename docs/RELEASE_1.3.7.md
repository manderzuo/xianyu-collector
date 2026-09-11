# 闲鱼管理系统 1.3.7 发布说明

- 版本：`1.3.7`
- 构建 ID（本地标记）：`20260911-1.3.7-cloud-account-sync`
- 基线：`v1.3.6`
- 前置批次：`docs/RELEASE_1.3.6.md`、`docs/RELEASE_1.3.5_ENTITLEMENTS_AND_APPROVAL_FIX.md`

1.3.7 修复「注册的账号不在云端 → 升级到 1.3.5+/1.3.6 后无法登录」这一线上阻断问题，
并完成存量账号自动导入云端。

## 1. 问题与根因

### 1.1 症状

用户反馈两类互相关联的故障：

- 注册时报「注册功能已关闭，请联系管理员」（已在 1.3.6 修复，属另一成因）；
- 登录时报「用户名或密码错误」，且**同一台电脑此前一直正常，升级后才失败**。

### 1.2 根因（两条历史线索交叉验证）

**线索一：1.3.5 是登录链路的分水岭。**

逐版本核对 `backend/app/api/routes/auth.py`：

| 版本 | 登录是否以云端为密码权威 |
| --- | --- |
| v1.3.0 ~ v1.3.4 | **否**（一律用本机 MySQL 的 `password_hash` 校验） |
| v1.3.5、v1.3.6 | **是**（`_authenticate_cloud_user` 调云端 `login`） |

1.3.5 之前，云端有没有这个账号无所谓；1.3.5 之后，云端没有的账号一律登不上。
因此「之前一直没问题、升级后失败」。

**线索二：注册从 1.3.0 起就不再写入云端。**

- `1bf112c`（9月7日）引入云端注册转发，注释明确：
  > In cloud mode the invite belongs to the shared auth service. Do not look it up in
  > this computer's business database, otherwise **a code issued on the administrator's
  > computer can never work on another computer.**
- `4586378`、`310ecc2`、`a2ae066` 均保留该转发；
- **`3578c49`（`feat: consolidate business features for Xianyu 1.3.0`）把它删除了**，
  之后所有版本都不再转发。

结果：**1.3.0 之后通过界面注册的账号只写本机 MySQL，云端永远没有该记录。**
再叠加线索一，升级到 1.3.5+ 后这些账号就全部登不上了。

### 1.3 线上实测证据

| 探针 | 结果 |
| --- | --- |
| `GET /api/xianyu/auth/health` | `200` `{"ok":true,"service":"xianyu-auth"}` |
| `POST /auth/login`（不存在的账号） | `401` `{"code":"invalid_credentials","message":"账号或密码错误"}` |
| `POST /auth/register`（无效邀请码） | `400` `{"code":"invalid_invite",...}`（端点存活） |

另外穷尽后端全部 `cloud_auth_request` 调用点（14 处），只有
`health / me / login / list_users / set_user_plan / disable_user / approve_user /
reject_user / entitlements / diagnostics / sync_invites`，**没有任何用户上传动作**；
`ensure_external_user` 零调用点，是死代码。确认「注册不会进云端」。

## 2. 修复内容

### 2.1 恢复云端注册转发（`backend/app/api/routes/auth.py`）

云端模式下 `/api/v1/auth/register` 转发到云端 `register`，在云端建立**待审**账号，
由管理员在任意电脑审批后该账号才可登录。本机不再创建只存在于本地的账号。

### 2.2 新增邀请码同步（关键前置条件）

注册转发到云端后，**邀请码也必须在云端存在**，否则用户在别的电脑注册会提示
「邀请码无效」。而 `sync_invites` 此前从无调用点。

- 新建邀请码时立即同步到云端（`registration_invites.py`）。
- 云端 `sync_invites` 扩展为可接受 **`code_hash` 直接同步**：存量邀请码在本机
  **只保存了哈希**（`code_encrypted` 列虽存在但从未写入，无法解密），因此按哈希同步。
  两端 `hash_invite_code` 均为 `sha256(normalize(code))` 十六进制，**格式完全一致**，
  云端校验行为与新建邀请码相同。
- 云端同步邀请码时**不回退状态**：已存在的邀请码不会被本机的旧状态覆盖，
  避免已核销的邀请码被重复使用。

### 2.3 存量账号自动导入云端

新增 `common/services/cloud_user_sync.py` 与云端 `import_users` 动作。

**哈希转码（本次修复的关键技术点）：** 两侧 KDF 参数完全相同，只是序列化格式不同：

| 侧 | 格式 |
| --- | --- |
| 本机 | `pbkdf2$<salt 明文>$<hex digest>` |
| 云端 | `pbkdf2_sha256$<iterations>$<b64url salt>$<b64url digest>` |

两者都是 PBKDF2-HMAC-SHA256 / 120000 次，因此 **salt 与 digest 可以直接搬运，
无需明文密码**。`local_hash_to_cloud()` 完成该转码，并有测试用**云端真实实现**
（直接导入 `deploy/cloud_auth/auth_store.py`）验证转码结果能够通过校验。

**安全约定（均被测试锁定）：**

- **只创建云端不存在的账号，绝不覆盖已有记录。** 云端是密码与权限的权威来源，
  管理员可能已在云端改过密码或状态。
- `admin` 保留账号同样不被覆盖。
- 只接受**云端格式**的 `password_hash`，拒绝本机格式，避免写入永远无法验证的哈希。
  本方法不接收明文密码。
- 导入的状态映射：本机 `1→approved`、`2→pending`、`0→disabled`，
  与云端审批语义一致（导入为 pending 的账号仍需审批才能登录）。

**触发时机（两条路径，互补）：**

1. **管理员登录后自动补录**（无需任何配置）。管理员登录时用其云端会话令牌调用
   `sync_users` 与 `sync_invites`，把本机存量账号与邀请码补进云端。
   每进程只执行一次，失败只记日志、不阻断登录。
2. **后端启动时自动导入**（需配置 `XIANYU_CLOUD_SYNC_SECRET`）。启动时没有用户会话，
   只能靠共享密钥证明身份。

### 2.4 云端 `sync_users` 鉴权（失败关闭）

二选一，任一通过即可：管理员云端会话令牌，或匹配 `XIANYU_CLOUD_SYNC_SECRET` 的共享密钥。

**未配置共享密钥时只接受管理员会话** —— 不会因为漏配环境变量而变成任何人可写云端账号。
该动作在会话校验之前处理，因为采集端后端启动时没有用户会话可用。

### 2.5 本机镜像补齐 email

云端登录成功后创建/更新本机镜像时，**此前没有写入 `email`**，导致「邮箱密码」页签
在云端模式无法把邮箱解析成账号名，只能报「用户名或密码错误」。现在：

- 云端 `app_users` 新增 `email` 列（可空，兼容旧库自动迁移）；
- 云端 `register` / `_public_user` 支持并返回 email；
- 本机镜像创建时写入、更新时仅在能拿到邮箱时补写（不清空已有邮箱）。

### 2.6 前端提示文案

云端注册只创建**待审**账号，通过后才能登录。因此：

- 注册页不再提示「注册成功，请登录」，改为优先展示后端返回的说明；
- 登录页内嵌注册同样优先展示后端消息。

## 3. 已验证的自动更新/回归

### 3.1 本版验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| 前端类型检查 | `tsc --noEmit` | 通过 |
| 前端 Lint | `eslint . --max-warnings 0` | 通过 |
| 前端构建 | `vite build`（APP_VERSION=1.3.7） | 通过 |
| Python 语法 | `compileall backend common deploy` | 通过 |
| 后端测试 | `unittest discover -s backend/tests -t .` | **101 passed** |

新增 `backend/tests/test_cloud_user_sync.py`（29 个用例），关键断言：

- **本机哈希转码后可被云端真实实现校验通过**（直接导入部署端代码，非复制品）；
- 转码后拒绝错误密码；云端格式原样透传；不可解析值返回 None；
- 导入后账号**能真正登录**（`store.authenticate` 成功）；
- 导入幂等；**不覆盖云端已有密码**；**不覆盖 admin**；
- 拒绝本机格式哈希、非法状态、空/超大批次；
- 导入为 pending 的账号仍无法登录；
- `sync_users` 鉴权：无密钥 401、密钥错误 401、非管理员 403、正确密钥 200 且导入成功；
- 云端注册转发确实发往云端且**不在本机建号**；云端错误状态码正确透传；
- 本机与云端对同一邀请码算出**同一个哈希**。

## 4. 部署要求（**顺序重要**）

> **必须先把 `deploy/cloud_auth/` 部署到云端服务器，再让客户端升级到 1.3.7。**

原因：客户端 1.3.7 会调用云端的 `sync_users` 动作。**当前线上云端还没有这个动作**，
若客户端先升级，导入会以 404 失败（只记日志，不影响登录），存量账号仍无法登录。

1. 部署云端鉴权服务（`deploy/cloud_auth/` 整体，含 `auth_store.py`、`server.py`）：

```bash
sudo python3 deploy/cloud_auth/install.py
sudo systemctl restart xianyu-auth
curl -s https://www.gemstory.cn/api/xianyu/auth/health
```

2. **（可选，推荐）** 在云端与客户端 `.env` 配置**同一个**共享密钥，
   让各机器启动时自动导入，无需管理员逐台登录：

```bash
# 云端服务器
XIANYU_CLOUD_SYNC_SECRET=<一段随机字符串>

# 每台客户端 app/.env（1.3.7 已加入 docker-compose 透传）
XIANYU_CLOUD_SYNC_SECRET=<同一段随机字符串>
```

未配置时同样可修复：**每台机器上用管理员账号登录一次**，即可触发存量账号与邀请码补录。

3. 客户端升级到 1.3.7。`email` 列由云端首次运行自动 `ALTER TABLE` 添加，无需手工迁移。

## 5. 需要人工确认的两个风险

### 5.1 admin 密码可能与云端不一致

云端 `app_users` 的 `admin` 是在云端首次安装时从当时采集端复制过去的。若此后有人在
某台机器上改过本机 admin 密码，那么**该机器必须改用云端那套 admin 密码**。

导入逻辑**故意不覆盖 admin**，因此不会自动解决这种情况。如果管理员登录失败，
需要在云端服务器上重置 admin 密码（直接改云端 SQLite 的 `app_users.password_hash`），
或提供一次性的重置脚本。

### 5.2 存量邀请码按哈希同步，但无法查看

存量邀请码的明文从未落库，因此只能按哈希同步——**功能正常**（用户手上的码仍然有效），
但管理员无法在云端界面查看明文。预览串仍然可见。

## 6. 涉及文件

云端（`deploy/cloud_auth/`，**需单独部署**）：

- `auth_store.py`：`email` 列与迁移、`_email()`、`register(email=...)`、`_public_user` 返回 email、
  `import_users()`（新增，含 `is_cloud_password_hash` 与批量上限）、`sync_invites()` 支持 `code_hash`
- `server.py`：`sync_users` 动作与 `_handle_sync_users`（双路径鉴权，失败关闭）、`register` 传 email

后端：

- `common/services/cloud_user_sync.py`（新增：哈希转码、负载构造、用户/邀请码同步）
- `backend/app/api/routes/auth.py`（注册转发、镜像 email、管理员登录后补录）
- `backend/app/api/routes/registration_invites.py`（新建邀请码同步云端）
- `backend/main.py`（启动时导入，需共享密钥）
- `backend/tests/test_cloud_user_sync.py`（新增 29 个用例）
- `docker-compose.yml`（透传 `XIANYU_CLOUD_SYNC_SECRET`）

前端：

- `frontend/src/pages/auth/Register.tsx`、`frontend/src/pages/auth/Login.tsx`（待审文案）

版本文件：`VERSION.txt`、`BUILD_ID.txt`、`frontend/package.json`、`frontend/package-lock.json`

## 7. 验收清单

| # | 验收项 | 期望结果 |
| --- | --- | --- |
| 1 | 云端部署后 `GET /api/xianyu/auth/health` | `{"ok":true,"service":"xianyu-auth"}` |
| 2 | 管理员在任一机器登录一次 | 日志出现 `cloud backfill after admin login users=... invites=...` |
| 3 | 用**此前登不上的存量账号**登录 | **登录成功**（本版核心验收点） |
| 4 | 管理员用户管理页 | 能看到导入的存量账号 |
| 5 | 用新邀请码在**另一台**电脑注册 | 提交成功，云端管理页出现待审申请 |
| 6 | 管理员审批通过后 | 该用户可正常登录 |
| 7 | 未审批前尝试登录 | 提示「账号正在等待管理员审批」 |
| 8 | 用「邮箱密码」页签登录（该账号有邮箱） | 登录成功 |
| 9 | 重复执行导入 | 不产生重复账号，已存在的被跳过 |
| 10 | VIP 用户开启内置 AI 自动回复 | 出现 336 条预置关键词（历史回归项） |

自动更新与运行时镜像同步的 7 项验收见
`docs/RELEASE_1.3.5_ENTITLEMENTS_AND_APPROVAL_FIX.md` §6.1。

## 8. 发布

```powershell
npm ci --prefix frontend
$env:APP_VERSION='1.3.7'
npm run build --prefix frontend
python -m compileall -q backend common deploy
python -m unittest discover -s backend/tests -t .
git tag -a v1.3.7 -m "Xianyu 1.3.7"
git push origin v1.3.7
```

发布顺序（交接文档 §7）：先上传客户端包与签名，最后上传 `latest.json`；
发布清单必须使用 RSA-SHA256 签名。
