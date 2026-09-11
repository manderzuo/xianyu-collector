# 闲鱼管理系统 1.3.6 发布说明

- 版本：`1.3.6`
- 构建 ID（本地标记）：`20260911-1.3.6-registration-gate-fix`
- 基线：`v1.3.5`（`codex/release-1.3.3-client-handoff-fix`）
- 前置批次：`docs/RELEASE_1.3.5_ENTITLEMENTS_AND_APPROVAL_FIX.md`

1.3.6 在 1.3.5 基础上完成「套餐权限独立界面」并修复 4 个线上功能性缺陷。

## 1. 本版修复的问题

### 1.1 注册被误判关闭：用户填完表单必然被拒（本版主要修复）

**现象**：用户打开注册页可以正常填写表单，提交后收到
「注册功能已关闭，请联系管理员」。

**根因**：两个接口对同一开关使用了**不同的默认值**，且
`xr_system_settings` 表中**从来没有种子数据写入 `registration_enabled`**，
所以“设置行缺失”是全新部署的默认状态：

| 接口 | 设置行缺失时的行为 |
| --- | --- |
| `GET /api/v1/system-settings/public` | `values.setdefault("registration_enabled", "true")` → 返回 **true**，前端显示注册表单 |
| `POST /api/v1/auth/register` | 读取到 `None` → 不在真值集合中 → 返回 **403 注册功能已关闭** |

因此用户能看到表单、能填完，但提交必然失败。这也解释了为什么只有部分用户
遇到：只有在管理员从未保存过“系统设置”的部署上，该设置行才不存在。

**修复**：新增共享模块 `common/services/system_settings.py`，公开设置接口与
注册接口都调用同一个 `registration_enabled()`，两者在代码层面不可能再分歧，
并由测试对全部取值逐一断言两者结论一致。

同时消除一个同类隐患：数据库里若存 `yes` / `on`，后端原本允许注册，
而前端只识别 `true` / `'true'`，会隐藏注册入口；现在公开接口统一输出
规范化 `"true"` / `"false"`，前端也保持宽容解析。

**取值语义**（已在 `parse_bool_value` 中文档化并由测试覆盖）：

- `true` / `1` / `yes` / `on` → 开放
- `false` / `0` / `no` / `off` → 关闭
- 缺失 / 空值 / 无法识别 → 回落到默认值 **开放**

默认开放是安全的：注册本身仍必须提供管理员发放的有效邀请码，邀请码才是
真正的准入控制。默认关闭会让全新部署完全无法注册。

> 管理员若要显式关闭注册，在「系统设置」里关闭该开关即可，会写入
> `false` 并严格生效。

### 1.2 套餐权限接口从未注册，页面必然取不到数据（P0）

`backend/app/api/routes/admin_entitlements.py`（264 行、7 个接口）从未在
`backend/main.py` 中 `include_router`，所有 `/api/v1/admin/entitlements/*`
请求都落到 `legacy_surface` 兜底路由 `/api/v1/{full_path:path}` 被当作
`FeatureRecord` 处理。

修复：注册该路由，且必须位于 `/api/v1` 兜底路由**之前**；并新增防回归测试
断言 7 条路径已注册且顺序正确。

### 1.3 注册申请待审批被错误显示为“正常”

`Users.tsx` 的 `statusLabelMap` 缺少 `PENDING` 键，`|| '正常'` 回落到“正常”，
徽标颜色也回落到绿色 ACTIVE。同时后端 `/approve`、`/reject` 接口虽存在，
但前端从未调用，页面上没有任何审批入口。

修复：补 `PENDING` 状态映射（蓝色「待审批」）、新增审批 API 与「通过 / 拒绝」
按钮、显示待审批数量提示。

> 该修复只让状态正确显示，**不会自动批准历史待审批账号**，需管理员手动点「通过」。

### 1.4 调整 VIP 套餐报“请在统一认证服务中修改”

`frontend/src/api/admin.ts` 的 `mapAdminUser` 未映射 `cloud_mode`，导致
`isCloudEdit` 恒为 `false`，编辑弹窗走本地分支提交完整资料，而后端云端模式
只接受 `{plan, plan_code, plan_expires_at, expire_at}`，收到多余字段返回 409。

修复：补齐 `cloud_mode` / 套餐字段映射；云端账号编辑弹窗改为只读并明确提示。

### 1.5 单用户功能授权在本机不生效

本机 `get_effective_entitlement` 只读本机覆盖表，而云端 `set_user_feature`
只写云端 `entitlements_json`。修复：登录时 `_sync_cloud_overrides()` 把云端
授权镜像到本机。

安全约束（已被测试锁定）：**只有云端明确返回 `entitlements` 字典时才执行清理**，
旧版鉴权服务不返回该字段时保持本机授权不变，否则每次登录都会清空用户授权；
镜像过程不修改 `auth_version`，避免作废已签发令牌。

### 1.6 个人设置看不到套餐详情

`/api/v1/users/me` 原本只返回 `expire_at`。修复：补齐 `plan_code` /
`plan_expires_at` / `entitlements`，个人设置新增只读套餐详情区块。

## 2. 套餐权限独立界面（交接文档 9.1）

`/admin/entitlements` 现为独立套餐权限中心，分「套餐配置」与「用户授权」两个页签，
覆盖 VIP 开通、套餐变更、套餐到期时间、单用户功能授权，并支持「跟随套餐」
删除单用户覆盖。

用户管理的套餐编辑入口已移除：`UserFormModal` 的套餐下拉与到期日输入删除，
改为只读展示 + 跳转按钮；`Users.tsx` 到期日列改为只读套餐列并提供跳转
`/admin/entitlements?tab=users&user_id=<id>`。

## 3. 验证结果

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| 前端类型检查 | `tsc --noEmit` | 通过 |
| 前端 Lint | `eslint . --max-warnings 0` | 通过 |
| 前端构建 | `npm run build`（APP_VERSION=1.3.6） | 通过 |
| Python 语法 | `compileall backend common deploy` | 通过 |
| 后端测试 | `unittest discover -s backend/tests -t .` | **72 passed** |

新增测试：

- `backend/tests/test_registration_gate.py`（13 个用例）——核心断言是
  “公开设置接口与注册接口对同一取值结论必须一致”，逐值遍历
  `None/true/false/1/0/yes/no/on/off/maybe`。
- `backend/tests/test_admin_entitlements.py`（19 个用例）——含路由注册防回归断言。

## 4. 部署前置条件

1. **必须重新构建并发布后端镜像**：1.1–1.6 的修复都在后端，仅更新客户端包不生效。
2. **确认云端鉴权服务已部署 `deploy/cloud_auth/`**：本版未改动该目录。按交接文档
   §5.2，若线上鉴权服务不由主流水线自动部署，其账号会话阻断（410）与
   `entitlements` 字段返回需该目录单独上线。
3. 历史待审批账号需管理员在用户管理页手动点「通过」。

## 5. 测试机验收清单

完整 21 项见 `docs/RELEASE_1.3.5_ENTITLEMENTS_AND_APPROVAL_FIX.md` 第 6 节。
本版新增/重点：

| # | 验收项 | 期望结果 |
| --- | --- | --- |
| A | 全新部署（未保存过系统设置）打开注册页并提交有效邀请码 | **注册成功**，不再出现“注册功能已关闭”（本版主要修复） |
| B | 管理员在系统设置中关闭注册 | 注册页显示“注册功能已关闭”，提交被拒（严格生效） |
| C | 再次开启注册 | 注册页恢复表单，提交可成功 |
| D | 打开 `/admin/entitlements` | 能看到套餐与功能矩阵（修复前为空白） |
| E | 给用户开通 VIP | 保存成功，无 409 报错 |
| F | 注册申请的账号在用户管理页 | 显示蓝色「待审批」，点「通过」后用户可登录 |
| G | 用户端「我的设置」 | 显示套餐编码、到期日与各功能配额 |
| H | VIP 用户开启内置 AI 自动回复 | 出现 336 条预置关键词规则 |

自动更新与运行时镜像同步的 7 项验收（1.2.0 / 1.3.4 测试机）见
`docs/RELEASE_1.3.5_ENTITLEMENTS_AND_APPROVAL_FIX.md` §6.1，验收命令同该节。

## 6. 涉及文件（1.3.6）

后端：

- `common/services/system_settings.py`（新增，注册开关唯一来源）
- `backend/app/api/routes/auth.py`（注册开关校验、云端授权镜像）
- `backend/app/api/routes/legacy_compat.py`（公开设置输出规范化）
- `backend/main.py`、`backend/app/api/routes/admin_entitlements.py`、`admin_users.py`、`user_profile.py`
- `backend/tests/test_registration_gate.py`（新增）、`test_admin_entitlements.py`（新增）、`test_cloud_auth_login.py`

前端：

- `frontend/src/api/auth.ts`（注册状态宽容解析）
- `frontend/src/pages/admin/Entitlements.tsx`、`Users.tsx`、`UserFormModal.tsx`
- `frontend/src/api/admin.ts`、`entitlements.ts`、`settings.ts`
- `frontend/src/pages/personalSettings/PersonalSettings.tsx`

版本文件：`VERSION.txt`、`BUILD_ID.txt`、`frontend/package.json`、`frontend/package-lock.json`

## 7. 发布

```powershell
npm ci --prefix frontend
$env:APP_VERSION='1.3.6'
npm run build --prefix frontend
python -m compileall -q backend common deploy
python -m unittest discover -s backend/tests -t .
git tag -a v1.3.6 -m "Xianyu 1.3.6"
git push origin v1.3.6
```

发布顺序（交接文档 §7）：先上传客户端包与签名，最后上传 `latest.json`；
发布清单必须使用 RSA-SHA256 签名。
