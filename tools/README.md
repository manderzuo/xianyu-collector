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
