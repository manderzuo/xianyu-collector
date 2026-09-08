# 闲鱼客服对话包生成器

运行：

```powershell
python tools/dialogue_pack_generator.py
```

如果已经保存过配置，可以使用 `python tools/dialogue_pack_generator.py --auto-start` 启动 GUI 并自动开始上次配置的任务。

工具使用两个 OpenAI 兼容 API：一个模拟买家，一个模拟卖家。勾选“买家和卖家共用同一套 API”时只需填写卖家配置。每个场景最多可生成 1000 个变体，每场最多 30 回合；界面启动时会估算 API 调用量。生成后会在输出目录写入：

GUI 中可以分别限制买家、卖家和模板整理阶段的单次输出 Token。建议从买家 40～80、卖家 80～120、整理 300～600 开始；这只限制单次输出，不能替代对变体数和回合数的总量控制。

API 地址、模型、代理和任务参数会保存到当前 Windows 用户的 `%LOCALAPPDATA%\XianyuDialoguePackGenerator\config.json`。API Key 使用 Windows DPAPI 按当前用户加密；不会写入仓库、对话包或运行日志。点击“清除已保存配置”可以删除本机配置。

- `dialogue_pack.partial.json`：后台运行中的增量结果，任务中断后仍可查看。
- `dialogue_pack_时间.json`：完整对话包，包含商品事实、原始多轮对话、生成统计和模板。
- `templates_时间.jsonl`：一行一个本地模板，适合后续导入模板库。
- `keywords_import_时间.json`：按现有关键词规则整理的候选导入文件，启用前必须人工审核。

## 建议填写方式

商品事实中填写真实信息，例如：

```text
售价：9.90 元
库存：以本地未使用卡密数量为准
交付：买家付款后自动发送卡密
有效期：2026 年 12 月 31 日前激活
售后：卡密无效先核验订单，确认问题后换卡；未知情况转人工
```

API 地址填写兼容 OpenAI Chat Completions 的服务根地址，例如 `https://api.openai.com/v1`。工具会向 `/chat/completions` 发请求。API Key 不会写入导出文件。

生成结果是合成内容，不是事实来源。导入现有系统前应先检查价格、库存、有效期、退款承诺和变量是否正确。

## 离线安装包

先使用 `build-windows-installer.ps1` 生成便携安装目录，再将本机已经验证过的
业务镜像和基础镜像放入安装包：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\build-windows-installer.ps1 `
  -OutputDirectory .\xianyu-one-click-installer `
  -Force -IncludeDockerImages
```

该模式把镜像导出到 `resources\images`，安装时由 `install.bat` 自动校验并导入。
业务镜像使用根文件系统导出，以兼容 Docker Desktop 的 containerd 镜像存储；
MySQL 和 Redis 使用标准 Docker 镜像归档。导出校验失败会中止打包，不会生成可疑的
小型空归档。

## 服务器迁移

服务器迁移脚本位于 `deploy/server-migration`。导出和导入前请先做腾讯云快照，
并保留旧服务器到新服务器验收完成：

```bash
sudo bash ./deploy/server-migration/server-migration-export.sh \
  --app-root /path/to/xianyu \
  --output /tmp/xianyu-migrations

sudo bash ./deploy/server-migration/server-migration-import.sh \
  --app-root /path/to/xianyu \
  --bundle /tmp/xianyu-migrations/xianyu-migration-....tar.gz.enc

sudo bash ./deploy/server-migration/server-healthcheck.sh \
  --app-root /path/to/xianyu
```

迁移包包含加密的数据库、业务文件、云端认证数据和部署文件；默认不会修改 DNS，
也不会在目标目录已有内容时强制覆盖。

## 更新签名密钥

正式发布前只生成一次密钥，并把私钥安全保存到 GitHub 仓库的
`UPDATE_SIGNING_PRIVATE_KEY` Actions secret；私钥不要提交到仓库或复制到客户电脑：

```powershell
python .\tools\generate-update-signing-key.py `
  --output-dir "$env:USERPROFILE\xianyu-release-signing-key"
```

将输出目录中的 `update-signing-public.xml` 复制为
`deploy\update-signing-public-key.xml` 后再构建客户安装包。客户端只携带公钥，
发布工作流使用私钥生成 `latest.json.sig`，客户端用 RSA-SHA256 校验清单后才会更新。
如果 GitHub secret 未配置，发布工作流会在上传前失败，不会切换线上清单。
