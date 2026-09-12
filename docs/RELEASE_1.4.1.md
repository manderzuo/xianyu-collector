# 闲鱼管理系统 1.4.1 发布说明

- 版本：`1.4.1`
- 构建 ID（本地标记）：`20260911-1.4.1-qr-token-and-cloud-password`
- 基线：`v1.4.0`
- 前置批次：`docs/RELEASE_1.4.0.md`

1.4.1 修复两个线上反馈的缺陷：**添加账号报「连接服务拒绝请求」**（并连带导致人脸核验二维码不弹出）
与**管理员改密码无法同步云端**（连同默认密码门禁形成死循环）。

> **状态：已完成修复与验证，未推送、未发布。** 交付物为本地提交，等待实测确认。

## 1. 问题 2：添加账号报「连接服务拒绝请求」

### 1.1 根因（P0）：`qr_login.py` 漏发内部令牌

连接服务对全部 `/internal/` 接口做部署级令牌校验（`websocket/main.py:28-35`）：

```python
@app.middleware("http")
async def protect_internal_routes(request, call_next):
    if request.url.path.startswith("/internal/"):
        token = request.headers.get("X-Internal-Token", "")
        if not token or not secrets.compare_digest(token, settings.jwt_secret):
            return JSONResponse(status_code=401, content={"detail": "内部调用凭证无效"})
```

全仓库共 39 处发送该令牌，**`qr_login.py` 一处都没有**——它是唯一漏发的调用方：

- `_notify_account_runtime`（`/internal/accounts/{id}/{start|restart}`）
- `_get_runtime_status`（`/internal/accounts/{id}/status`）

**实测确认**（本地运行环境内直接调用）：

```
无令牌 -> 401 {"detail":"内部调用凭证无效"}
带令牌 -> 200 {"success":true,...}
```

**引入原因**：`310ecc2`（09-07）加入中间件，`6835f5e`（09-10）在**没有中间件**的分支基线上新增了
QR 运行时校验调用，`git merge-base` 显示两者**互不为祖先**——跨分支合并遗留的回归。

### 1.2 错误信息吞掉了真实原因

`qr_login.py` 原实现：

```python
return {"status": "failed", "detail": payload.get("message", "连接服务拒绝请求")}
```

401 响应体是 `{"detail": "内部调用凭证无效"}`——**只有 `detail`，没有 `message`**，
于是走到兜底值，把真实原因替换成含糊的「连接服务拒绝请求」，导致排查困难。

**修复**：新增 `_runtime_failure_message()`，依次读 `message` / `detail` / `error`
（含嵌套字典），并把 `http_status` 一并带出。

### 1.3 人脸核验二维码不弹出（连带后果）

`qr_login.py` 的失败分支**提前 return**：

```python
runtime = await _notify_account_runtime(...)      # ← 401 失败
if runtime.get("status") in {"failed", "unavailable"}:
    item.status = "failed"
    account.status = "expired"
    return ok(...)                                 # ← 直接返回
```

而人脸信息是在**后面**才从 `qr_login_manager.status()` 取出来赋给 `item` 的：

```python
state = qr_login_manager.status(session_id)
item.verification_url = state.get("verification_url")
item.face_qr_url = state.get("face_qr_url")
```

**提前 return 导致这一段永不执行 → `face_qr_url` 永远为空 → 前端
`if (result.face_qr_url)` 不成立 → 人脸二维码不显示。**

顺带确认：`account.status = "expired"` 会把**已成功保存 Cookie** 的新账号标记为过期。

### 1.4 人脸核验自身的两个缺陷（一并修复）

`backend/app/services/qr_login.py` 的 `_monitor`：

1. **竞态窗口**：原实现先置 `session.status = "verification_required"`，再 `await _run_face_verification()`；
   而 `face_qr_url` 是在后者**内部**才设置的。前端在窗口期内轮询会拿到
   「需要人脸验证」但拿不到图。
2. **异常直接退出循环**：`_run_face_verification` 抛错时被通用 `except` 捕获并设 `session.error`，
   但此时状态已是 `verification_required`，**不再满足循环条件 → 循环退出**，
   状态永久停住、既无二维码也不报错。

**修复**：

- 新增中间态 `face_verifying`；**二维码就绪后才**置 `verification_required`
- 人脸分支异常显式落到 `failed` 并写入 `人脸验证失败：…`
- `status()` 改为**只要拿到过就回传** `face_qr_url` / `verification_url`，
  不再与状态强绑定，避免轮询间隔错过

## 2. 问题 1：管理员改密码与默认密码门禁

### 2.1 缺陷 A：改密码完全没有上传云端

`auth.py` 原 `change_password` 只写本机 `xr_users.password_hash`，**函数体内没有任何云端调用**。
而自 1.3.5 起登录的密码权威是云端，因此**新密码在登录时无效**。

云端连改密码接口都没暴露：`server.py` 支持的 22 个动作里没有 `change_password`，
但**数据层其实早就写好了** `auth_store.reset_password()`——全仓库零调用方，是半成品。

**修复**：

- 云端新增 `AuthStore.change_password(user_id, old_password, new_password)`：
  **必须校验旧密码**、拒绝新旧相同、拒绝过短新密码（与管理员用的 `reset_password` 分开）
- 云端新增 `AuthStore.revoke_user_sessions(user_id)`：改密码后**作废该账号全部会话**
- 云端 `server.py` 暴露 `change_password` 动作，**放在会话校验之后**（自助入口必须有会话）
- 后端 `change_password` 在云端模式下**转发到云端**；成功后同步本机镜像；
  **云端失败时绝不改动本机密码**，避免两边不一致

### 2.2 缺陷 B：默认密码门禁与上述缺陷形成死循环

前端 `Accounts.tsx` 有一道**纯前端**门禁：查本机哈希是否为 `admin123`，是则拦截添加账号。

它作用于 **4 个入口**：扫码登录、兼职登录、账号密码、手动输入。

**后端没有这道限制**——全后端搜索 `DEFAULT_ADMIN_PASSWORD` 只有 3 处：常量定义、
`ensure_admin` 建号、以及只读的检查接口。**没有任何创建账号接口校验它。**

结合缺陷 A 形成死结：

| 用户选择 | 结果 |
| --- | --- |
| 不改密码 | 门禁拦截，无法添加账号 |
| 改密码 | 新密码云端没有 → 登不上；用旧密码登录后本机哈希被覆盖回去 → 门禁照样拦 |

（`auth.py` 在云端登录成功时执行 `user_record.password_hash = hash_password(password)`，
而 `password` 是登录输入的、已被云端校验通过的值，因此本机哈希总会被重置为云端密码。）

**修复**：**移除前端门禁**（连同不再使用的 `checkAdminDefaultPassword` 导入）。
后端无此逻辑，故不影响安全模型。个人设置页改为展示后端返回的真实结果
（云端模式下为「云端密码已更新，请重新登录」），不再让人误以为只在本机生效。

## 3. 验证结果

### 3.1 自动化测试

| 验证项 | 结果 |
| --- | --- |
| `compileall backend common websocket scheduler deploy` | 通过 |
| 后端测试 `unittest discover -s backend/tests -t .` | **197 passed**（新增 33） |
| 前端 `tsc --noEmit` | 通过 |
| 前端 `eslint --max-warnings 0` | 通过 |
| 前端 `vite build`（APP_VERSION=1.4.1） | 通过 |

新增 `backend/tests/test_internal_token_and_cloud_password.py`（33 个用例）覆盖：
错误映射读 `detail`、**两处内部调用都带令牌**（结构性保护）、401 显示真实原因、
人脸二维码先于状态就绪、`face_verifying` 中间态、异常落到 `failed`、
`status()` 只要拿到图就回传、云端改密码（正确/旧密码错/新旧相同/过短/未知用户）、
会话作废、云端动作位置在会话校验之后、后端转发与失败不改本机、本机模式行为、
**门禁已移除且添加入口未被连带删除**。

### 3.2 真实运行环境验证（本地）

**内部令牌修复**——在本地运行的容器内直接调用：

| 调用 | 修复前 | 修复后 |
| --- | --- | --- |
| `_notify_account_runtime` | `failed` /「连接服务拒绝请求」 | **`pending`** |
| `_get_runtime_status` | `failed` | **`ok`** |

**云端改密码**——本地起独立云端实例，真实 HTTP 全部通过：

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | 登录取会话 | 200 + token |
| 2 | 旧密码错 | **401 原密码不正确** |
| 3 | 新旧相同 | **400 新密码不能与原密码相同** |
| 4 | 正确改密码 | **200 密码已更新** |
| 5 | 用旧会话调 `me` | **401（会话已作废）** |
| 6 | 新密码登录 | 200 |
| 7 | 旧密码登录 | 401 |
| 8 | 无会话改密码 | 401 |

### 3.3 本地实测环境

本地部署（`E:\xianyu\app`，源码挂载模式）已更新到 1.4.1 并重启：

- 版本文件：`1.4.1` / `20260911-1.4.1-qr-token-and-cloud-password`
- 后端 / websocket / scheduler / frontend 四容器均 healthy
- 前端入口：`http://127.0.0.1:20002`，已引用新构建 `index-BJ1uKq0q.js`
- 覆盖前的原文件已备份到 `E:\xianyu\app\backup-1.4.0-to-1.4.1\`

## 4. 实测注意事项（重要）

**问题 2 可立即实测**：在本地界面点「添加账号」→ 扫码 → 应不再出现
「连接服务拒绝请求」；若闲鱼要求人脸核验，应能看到人脸二维码。

**问题 1 的云端路径本地无法实测**：本地部署的 `XIANYU_CLOUD_AUTH_URL` 指向
**生产云端**（`https://www.gemstory.cn`），而生产云端尚未部署本次新增的
`change_password` 动作 —— 因此本地点「修改密码」会返回「接口不存在」。

云端动作本身已用**本地独立实例 + 真实 HTTP** 验证通过（见 3.2），后端转发由单元测试覆盖。
若要在应用层端到端实测，需要二选一：

1. **先部署 `deploy/cloud_auth/` 到云端**（这是本次修复真正生效的前提，见第 5 节）
2. 或在本地另起一个云端实例并把本地部署指向它（需要临时调整 `.env`，涉及账号播种）

## 5. 部署依赖（问题 1 的前置条件，**尚未执行**）

**`deploy/cloud_auth/` 必须部署到云端，问题 1 才会真正修复。**
已核实主流水线（`.github/workflows/build-and-publish.yml`）**不包含任何
`cloud_auth` / `xianyu-auth` 部署步骤**，必须手工执行。

### 5.1 当前生产云端状态（已实测，2026-09-12）

```
service:              active
health:               {"ok": true, "service": "xianyu-auth"}
change_password:      auth_store=0  server=0   ← 本次新增，未部署
revoke_user_sessions: auth_store=0  server=0   ← 本次新增，未部署
reset_password:       auth_store=1  server=0   （数据层有，未暴露）
```

**结论：在云端部署完成之前，客户端升级到 1.4.1 也修不好问题 1**
（点「修改密码」会返回「接口不存在」）。问题 2 不受此影响，升级即生效。

### 5.2 部署路径（已探明）

| 项目 | 路径 |
| --- | --- |
| 云端代码 | `/opt/gemstory/xianyu-auth/`（`server.py`、`auth_store.py`、`admin.html`、`diagnostic_store.py`） |
| 数据库 | `/var/lib/gemstory/xianyu-auth/server.db` |
| 环境文件 | `/var/lib/gemstory/xianyu-auth/session.env` |
| systemd | `xianyu-auth`（User=xianyu-auth，WorkingDirectory=/opt/gemstory/xianyu-auth） |

### 5.3 部署步骤

```bash
# 1) 备份数据库与将被覆盖的文件（务必先做）
STAMP=$(date +%Y%m%d%H%M%S)
sudo cp /var/lib/gemstory/xianyu-auth/server.db /var/lib/gemstory/xianyu-auth/server.db.bak-$STAMP
sudo cp /opt/gemstory/xianyu-auth/auth_store.py /opt/gemstory/xianyu-auth/auth_store.py.bak-$STAMP
sudo cp /opt/gemstory/xianyu-auth/server.py     /opt/gemstory/xianyu-auth/server.py.bak-$STAMP

# 2) 上传两个文件（整目录上传，避免只传部分文件导致新旧混用）
scp -i ~/.ssh/Third.pem auth_store.py server.py \
    ubuntu@49.232.128.118:/tmp/xianyu-auth-141/
sudo cp /tmp/xianyu-auth-141/*.py /opt/gemstory/xianyu-auth/

# 3) 重启并验证
sudo systemctl restart xianyu-auth
sleep 2
systemctl is-active xianyu-auth
curl -s https://www.gemstory.cn/api/xianyu/auth/health
grep -c change_password /opt/gemstory/xianyu-auth/auth_store.py   # 期望 1
grep -c change_password /opt/gemstory/xianyu-auth/server.py       # 期望 1
```

> **历史陷阱**：曾出现「只上传部分文件」导致目录里混入陈旧 `admin.html`
> （3174 字节）的情况。整目录上传可避免。

### 5.4 部署后验证（8 项，与 3.2 节同口径）

用真实账号在**应用界面**点「修改密码」，逐项确认：

| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | 原密码填错 | 提示「原密码不正确」，密码未变 |
| 2 | 新密码与原密码相同 | 提示「新密码不能与原密码相同」 |
| 3 | 正确修改 | 提示「云端密码已更新，请重新登录」并退出登录 |
| 4 | 用**新**密码登录 | 成功 |
| 5 | 用**旧**密码登录 | 失败 |
| 6 | 改密码前的其他设备 | 会话失效，需重新登录 |
| 7 | 重新登录后检查账号列表是否可添加账号 | 正常（门禁已移除） |
| 8 | 云端用户数未变化 | 与部署前一致（本次不涉及用户表结构） |

### 5.5 回滚

```bash
STAMP=<部署时的 STAMP>
sudo cp /opt/gemstory/xianyu-auth/auth_store.py.bak-$STAMP /opt/gemstory/xianyu-auth/auth_store.py
sudo cp /opt/gemstory/xianyu-auth/server.py.bak-$STAMP     /opt/gemstory/xianyu-auth/server.py
sudo systemctl restart xianyu-auth
curl -s https://www.gemstory.cn/api/xianyu/auth/health
```

本次改动**不涉及数据库表结构变更**（`change_password` 只更新既有的
`password_hash` 列，`revoke_user_sessions` 只更新既有的 `revoked_at` 列），
因此回滚只需还原文件，**数据库备份仅为保险**。

## 6. 涉及文件

- `backend/app/api/routes/qr_login.py`：`_runtime_failure_message`、
  两处内部调用补 `X-Internal-Token`、`http_status` 透传
- `backend/app/services/qr_login.py`：`face_verifying` 中间态、二维码先于状态、
  异常落 `failed`、`status()` 宽松回传
- `backend/app/api/routes/auth.py`：`change_password` 云端转发 + 失败不改本机
- `deploy/cloud_auth/auth_store.py`：`change_password`、`revoke_user_sessions`
- `deploy/cloud_auth/server.py`：`change_password` 动作（会话校验之后）
- `frontend/src/pages/accounts/Accounts.tsx`：移除默认密码门禁（4 处调用 + 函数 + 导入）
- `frontend/src/pages/personalSettings/PersonalSettings.tsx`：展示后端真实结果
- `backend/tests/test_internal_token_and_cloud_password.py`（新增 33 个用例）

版本文件：`VERSION.txt`、`BUILD_ID.txt`、`frontend/package.json`、`frontend/package-lock.json`

## 7. 发布结果与遗留事项

### 7.1 发布结果（已完成）

流水线 run `34670465801` **success**，线上产物已验证：

| 项目 | 结果 |
| --- | --- |
| `latest.json` | `version 1.4.1`，`build_id 0b382b89961a-1.4.1` |
| 四个镜像 | `xianyu-{backend,websocket,scheduler,frontend}:1.4.1` 全部 HTTP 200 |
| 签名 | `latest.json.sig` HTTP 200 |
| 客户端包 | SHA-256 `2c684785…bc25` 与实际下载**逐字节一致**（8.96 MB） |
| 提交 / 标签 | `0b382b8` / `v1.4.1` |

### 7.2 1.4.0 撤销问题已随本次发布解决

原计划单独撤销 1.4.0，但两条路都走不通：

- **CI 降级守卫**：`refusing release downgrade: base=1.4.0, target=1.3.7`
  （`build-and-publish.yml:143-144`），重跑 v1.3.7 流水线在 `release_base` 步骤**失败**
- **无法手工造旧清单**：服务器无签名私钥，发布目录只保留当前 `latest.json`、无历史留档，
  手改会让签名校验失败被客户端拒绝（`update-xianyu-gui.ps1` 会抛
  「更新清单签名校验失败，已拒绝本次更新」）

**1.4.1 发布后 `latest.json` 已指向 1.4.1，1.4.0 不再提供给任何客户端**，
效果等同于撤销，且因 `1.4.1 > 1.4.0` 可通过降级守卫。

### 7.3 唯一遗留：云端尚未部署（见第 5 节）

问题 1 的云端动作**尚未部署到生产**，因此该问题当前仍未真正修复。
步骤与回滚见 5.3 / 5.5，部署后按 5.4 的 8 项验证。

> **已登记为遗留问题，推迟到下一次版本更新处理**：
> 见 `docs/KNOWN_ISSUES.md` 的 **KI-001**（云端未部署 → 改密码不可用）
> 与 **KI-002**（发布顺序缺约定 → 客户端可能先于云端上线）。
>
> 修复代码已包含在 v1.4.1 中并通过验证，**缺的只是部署这一步**，
> 下次发版时按 KI-001「修复方案 A」部署即可，无需再改代码。
