# 闲鱼客服对话包生成器

运行：

```powershell
python tools/dialogue_pack_generator.py
```

如果已经保存过配置，可以使用 `python tools/dialogue_pack_generator.py --auto-start` 启动 GUI 并自动开始上次配置的任务。

工具使用两个 OpenAI 兼容 API：一个模拟买家，一个模拟卖家。勾选“买家和卖家共用同一套 API”时只需填写卖家配置。每个场景最多可生成 1000 个变体，每场最多 30 回合；界面启动时会估算 API 调用量。生成后会在输出目录写入：

GUI 中可以分别限制买家、卖家和模板整理阶段的单次输出 Token。建议从买家 40～80、卖家 80～120、整理 300～600 开始；这只限制单次输出，不能替代对变体数和回合数的总量控制。若 API 返回因 `max_tokens`/`max_output_tokens` 截断，工具会自动把本次上限扩大后重试，最多重试 2 次；成功返回的消息不会再被本地按字符硬切。

API 地址、模型、代理和任务参数会保存到当前 Windows 用户的 `%LOCALAPPDATA%\XianyuDialoguePackGenerator\config.json`。API Key 使用 Windows DPAPI 按当前用户加密；不会写入仓库、对话包或运行日志。点击“清除已保存配置”可以删除本机配置。

- `dialogue_pack.partial.json`：后台运行中的增量结果，任务中断后仍可查看。
- `dialogue_pack_时间.json`：完整对话包，包含商品事实、原始多轮对话、生成统计和模板。
- `templates_时间.jsonl`：一行一个本地模板，适合后续导入模板库。
- `keywords_import_时间.json`：按现有关键词规则整理的候选导入文件，启用前必须人工审核。

运行日志中的“买家 … / 卖家 …”只显示短预览，超出时会标注“预览 36/总长度 字”；这不能代表导出内容被截断，完整文本以 JSON/JSONL 文件为准。

## 断点续传与续写

GUI 默认勾选“启用断点续传”。开始任务时会自动读取输出目录下的 `dialogue_pack.partial.json`；也可以在“指定已有包”中选择以前生成的 `partial` 或完整 `dialogue_pack_时间.json`。

- 已完成且输入内容未变化的会话会直接跳过，不重复消耗 API。
- 进行中或失败的会话会从最后一次保存的回合继续；如果中断在买家消息之后，恢复时会直接生成对应的卖家回复，不重复买家调用。
- 增加变体数时，原有有效会话会保留，只生成新增部分；如果修改最大回合数，相关会话会按新回合设置重新生成，避免输出结构不一致。
- 修改商品 ID、商品名称、事实、场景、最大回合数或是否整理模板后，会通过输入指纹识别变化，只重生成受影响的会话，防止旧商品信息混入。
- 每个回合均使用临时文件加原子替换保存，强制关闭或异常退出不会留下半截 JSON。再次点击“开始后台生成”即可继续。
- 同一个输出目录同时只能运行一个生成任务；程序会创建 `dialogue_pack.run.lock`，防止多个 GUI 实例互相覆盖断点文件。

旧版本没有输入指纹的包也支持兼容续传，但只有商品事实、回合数和模板整理设置都一致时才会复用；无法确认一致时会自动重新生成，保证内容安全。

## 建议填写方式

商品事实中填写真实信息，例如：

```text
售价：9.90 元
库存：以本地未使用卡密数量为准
交付：买家付款后自动发送卡密
有效期：2026 年 12 月 31 日前激活
售后：卡密无效先核验订单，确认问题后换卡；未知情况转人工
```

API 地址填写兼容 OpenAI 的服务根地址，例如 `https://api.openai.com/v1`。普通模型会向 `/chat/completions` 发请求；`muse-spark-*` 会自动向 `/responses` 发请求。API Key 不会写入导出文件。

生成结果是合成内容，不是事实来源。导入现有系统前应先检查价格、库存、有效期、退款承诺和变量是否正确。

## 离线蒸馏

不需要再次调用 API，可直接对已有生成包执行：

```powershell
python tools/dialogue_pack_distiller.py --input dialogue_packs/dialogue_pack_20260905_125944.json
```

蒸馏器会把内容按 34 个原始场景分组，每个场景默认保留最多 10 条高分、多样化回复；会过滤未完成会话、事实占位、元提示泄漏和需要人工接管的内容，并排除会命中多个不同回复的关键词。原始包不会被修改。

输出到 `dialogue_packs/distilled/`：

- `distilled_pack.json`：生产候选模板，仍需审核。
- `distilled_keywords_import.json`：只包含无冲突关键词的候选导入数据。
- `distilled_review.json`：失败会话、人工审核、占位内容、旧模板和无唯一关键词内容。
- `distillation_report.json`：来源、数量、冲突和质量统计。
- `distilled_templates.jsonl`：逐行模板文件。
