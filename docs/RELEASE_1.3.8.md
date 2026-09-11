# 闲鱼管理系统 1.3.8 发布说明

- 版本：`1.3.8`
- 构建 ID（本地标记）：`20260911-1.3.8-im-keepalive-device-id`
- 基线：`v1.3.7`
- 前置批次：`docs/RELEASE_1.3.7.md`

1.3.8 修复「IM 会话与设备指纹」两个根因，解决"自动续期看起来在跑却救不回账号"的问题。

## 1. 问题现象

线上账号（`xy298529811` / goofish_id `2223021364297`）出现：

```
网页接口获取 Token 失败：FAIL_SYS_SESSION_EXPIRED::Session过期
账号 7 长连接断开，将在 60.0 秒后重试
```

诊断结论：**网页登录态过期，不是被平台限制**（82 条日志全为 `SESSION_EXPIRED`，
无一条 `USER_VALIDATE` / `ILLEGAL_ACCESS` / punish，本地风控表 0 行，
且浏览器续期能成功拿到新 Cookie）。但自动续期每 5 分钟都在跑却始终救不回来。

## 2. 根因与修复

### 2.1 P0：缺少 IM 会话保活环节（本次主要修复）

对比公开实现 [XianYuApis](https://github.com/cv-cat/XianYuApis)（同类项目的上游底座，
README 明确宣称"Token 维持：自动刷新登录态，常驻进程不掉线"）后确认：它维护会话靠的是
**两个不同的 mtop 接口**。

| 用途 | 接口 | 频率 |
| --- | --- | --- |
| 取 WS 登录 token | `mtop.taobao.idlemessage.pc.login.token` | 建连时 |
| **会话保活** | **`mtop.taobao.idlemessage.pc.loginuser.get`** | **每 600 秒** |

它的实现（`goofish_apis.py:348` / `goofish_live.py:252`）：

```python
def refresh_token(self):                       # 保活
    self.refresh_token_url = '.../mtop.taobao.idlemessage.pc.loginuser.get/1.0/'
    ...
def user_alive(self):
    while True:
        time.sleep(600)
        self.xianyu.refresh_token()
async def main(self):
    threading.Thread(target=self.user_alive).start()   # 建连前就启动
```

**我们仓库里 32 个 mtop 接口名中完全没有 `loginuser.get`——全仓库零引用。**

我们原有三层机制名字都像"续期"，但没有一层具备保活能力：

| 机制 | 实际行为 | 层级 |
| --- | --- | --- |
| `refresh_cookies`（20 分钟） | Passport 三步续期 | Cookie 层 |
| `refresh_tokens`（30 分钟） | `POST /internal/accounts/{id}/restart`——**用同一份会话重新取 token** | 重连，非续期 |
| IM 运行时循环（5 分钟） | 失败后触发上面两条 | 兜底 |

`refresh_tokens` 本质只是"拿现有会话再换一个 token"，**若底层 mtop 会话已失效必然失败，
它不会让会话变新**。参考实现的 `user_alive()` 恰恰相反：在会话仍有效时定期"续命"。

**修复**：新增 `_request_web_keepalive()` 与 `_keepalive_loop()`，
在 `_run` 中随运行时启动、退出时回收，每 600 秒调用 `loginuser.get`：

- 成功 → 合并响应 Cookie（刷新 `_m_h5_tk` 等票据）并写回数据库，记录 `last_keepalive_at`；
- 失败 → 记录 `last_keepalive_error`，**不直接断开长连接**；
- 确认会话失效（命中 `is_session_expired_message`）→ 立即触发续期，不必等长连接自己断开；
- 单次异常不会终止循环（回归测试锁定）。

保活与重连链路都可能触发续期，因此新增**账号级 `renew_lock`** 串行化，
避免同一账号同时打两次 Passport。

### 2.2 P0：续期请求带着风控标记反复重试

[xianyu-pilot](https://github.com/dameng2026/xianyu-pilot) 的 CHANGELOG 记录了
必须清除的风险 Cookie 清单及其原因：

> `RISK_COOKIE_NAMES` 清单（`x5secdata` / `x5sec` / `x5sectag` / `x5pref` /
> `bx-cookie-test` / `tfstk` / `cbc` / `sca` / `isg`），刷新重试前清除 Baxia 通过
> Set-Cookie 重新设置的风险标记，避免"刷新→带 risk cookies→再次 punish→刷新"死循环

对照该账号的实际 Cookie，命中 4 个：`x5secdata`、`x5sectag`、`tfstk`、`cbc`。
**每一轮续期都带着这些"已被风控标记"的记号去打 Passport**，很可能正是长登录 Cookie
一直拿不到的原因之一。

**修复**：新增 `RISK_COOKIE_NAMES` 与 `strip_risk_cookies()`：

- `_api_renew_once` 开头先清除风险标记，确保请求回到干净状态；
- 最终 Cookie 以清理后的串为基准合并（原实现在此处用原始串，会把已剔除的标记带回来）；
- **平台本轮回写的风险标记也会被剔除**，不写入后续重试与数据库，从源头打断循环。

### 2.3 P1：设备指纹每次请求都变

参考实现（`goofish_live.py:22`）在 `__init__` 里生成设备指纹并全程复用：

```python
self.device_id = generate_device_id(self.myid)   # 只生成一次
```

我们`websocket/app/xianyu_runtime.py:64` 的 `_generate_device_id()` 用的是
`uuid.uuid4()`，且**每次取 Token 都调用一次**，即每次请求都换一个新设备指纹。
数据库里 `xr_accounts.im_device_id` 明明存着稳定值，但**全仓库零引用**（旧版本遗留）。

**设备指纹不断变化，是平台判定"请求来自不可信环境"的典型诱因。**

**修复**：

- `Account` 模型新增 `im_device_id` 列，并在 `common/db/session.py` 增加
  `_migrate_account_columns()` 为旧库补列（可空，不覆盖已有数据）；
- 新增 `_stable_device_id()`：已有值直接复用，缺失才生成并落库；
- `_resolve_runtime_device_id()` 解析并缓存到 `runtime.device_id`；
- `_request_token()` 增加 `device_id` 参数并按传入值使用；
  续期验收路径同样传入同一个指纹；
- 若落库失败，兜底生成的指纹也会在**进程内固定**，避免每次重连换指纹。

## 3. 可观测性

运行时状态新增字段，便于后台确认保活真的在工作：

```json
{
  "device_id": "7f8fd3c9-...-2223021364297",
  "last_keepalive_at": "2026-09-11T10:20:00+00:00",
  "last_keepalive_error": ""
}
```

日志新增：

```
账号 7 IM 会话保活成功
账号 7 IM 会话保活失败：FAIL_SYS_SESSION_EXPIRED::Session过期
续期前清除风险 Cookie：x5secdata,tfstk
账号 7 生成并固化设备指纹
```

## 4. 验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| Python 语法 | `compileall backend common websocket scheduler deploy` | 通过 |
| 后端测试 | `unittest discover -s backend/tests -t .` | **127 passed** |

新增 `backend/tests/test_keepalive_and_device_id.py`（26 个用例）：

- **保活接口**：成功合并 Cookie 并刷新票据；请求确实指向 `loginuser.get`
  且带 `api` / `sessionOption` / `sign`；`SESSION_EXPIRED` 正确上报；
  缺 `unb` 短路不发请求；网络异常与非 JSON 响应都返回错误而不抛异常。
- **保活循环**：成功记录时间戳且不触发续期；会话失效立即触发续期；
  **冷却期内只续期一次**（防高频）；非会话类错误不触发续期；
  单次异常不杀死循环；空 Cookie 跳过。
- **风险 Cookie**：清理风险标记并保留 `unb`/`cookie2` 等凭据；
  无风险标记时为空操作；**实际发给 Passport 的请求不含风险标记**；
  **平台回写的风险标记不会进入返回的 Cookie**。
- **设备指纹**：已有值复用且不标记新建；缺失时生成一次并稳定；
  不同账号指纹不同；`Account` 模型确实声明该列；
  **`_request_token` 原样沿用传入的指纹**。
- **接线保护**：`_run` 确实创建并回收保活任务；保活间隔为 600 秒。

## 5. 需要人工确认

1. **本次修复不会自动救回已过期的账号。** 那个账号仍需**重新扫码登录一次**；
   本版是为了让它以后不容易再枯死。
2. **建议同时填上账号登录密码**（账号设置里的 `username` / `login_password`，
   目前为空）。密码兜底路径此前因未配置而被跳过；填上后"必定要人工"可降为
   "多数情况能自动"。注意若闲鱼弹滑块/人脸，仍会返回
   `verification_required`（`websocket/app/browser_cookie_renew.py`）。
3. **无法保证 100% 不再过期。** `loginuser.get` 是参考实现经过验证的成熟做法，
   方向有据，但平台仍可能因其他原因让会话失效。

## 6. 遗留问题（本版未处理，建议后续排期）

以下问题已在 1.3.7 诊断中确认存在，但**不属于本次续期/指纹范围**，尚未修改：

| 问题 | 影响 |
| --- | --- |
| IM 失败即把账号标 `expired`，而网页侧任务按 `status == "active"` 过滤 | 网页侧自愈逻辑永久不可达，一次 IM 失败放大为整账号停摆 |
| 处理 0 个账号仍报 `completed`（`account_renewal.py:151`、`account_sync.py:75`） | 任务状态误导，看不清"其实什么都没干" |
| `cookie_next_renewal_at` / `cookie_last_renewed_at` 为死字段（代码零引用） | 界面/排查看到的"下次续期时间"是假的，真实调度是 cron |
| 未识别 punish / 验证类错误 | 撞上滑块时会误判为"需重新扫码"，而参考实现走滑块求解 |

## 7. 涉及文件

- `websocket/app/xianyu_runtime.py`：保活接口与循环、设备指纹解析与固化、
  `renew_lock` 串行化、Cookie 写库helper、运行时状态字段
- `common/services/cookie_renewal.py`：`RISK_COOKIE_NAMES`、`strip_risk_cookies`、
  续期前与合并后两次清理
- `common/models/accounts.py`：`im_device_id` 列
- `common/db/session.py`：`_migrate_account_columns` 旧库补列
- `backend/tests/test_keepalive_and_device_id.py`（新增 26 个用例）

版本文件：`VERSION.txt`、`BUILD_ID.txt`、`frontend/package.json`、`frontend/package-lock.json`

## 8. 部署与验收

```powershell
npm ci --prefix frontend
$env:APP_VERSION='1.3.8'
npm run build --prefix frontend
python -m compileall -q backend common websocket scheduler deploy
python -m unittest discover -s backend/tests -t .
```

`im_device_id` 列由后端启动时自动 `ALTER TABLE` 补齐，无需手工迁移。

| # | 验收项 | 期望结果 |
| --- | --- | --- |
| 1 | 重新扫码登录后等待 10 分钟以上 | 日志出现 `账号 N IM 会话保活成功` |
| 2 | 查询账号运行时状态 | `last_keepalive_at` 在更新，`last_keepalive_error` 为空 |
| 3 | 查询 `xr_accounts.im_device_id` | 有值，且**重启容器后保持不变** |
| 4 | 连续观察多轮续期日志 | 出现 `续期前清除风险 Cookie：...`，且不再反复 `SESSION_EXPIRED` |
| 5 | 长连接状态 | `connected_account_ids` 含该账号，聊天可正常收发 |
| 6 | 保活间隔确认 | 两次 `IM 会话保活成功` 间隔约 600 秒 |

历史回归项（自动更新、套餐权限、注册审批、336 条预置关键词）见
`docs/RELEASE_1.3.7.md` 与 `docs/RELEASE_1.3.5_ENTITLEMENTS_AND_APPROVAL_FIX.md`。
