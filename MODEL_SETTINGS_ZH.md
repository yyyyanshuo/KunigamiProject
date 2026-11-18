# 线路与模型设置

个人主页的「线路与模型设置」统一管理模型 API Key 和九项功能模型。

- Gemini、ChatGPT、Claude、Grok 使用各自官方 API；自填写支持 OpenAI Chat Completions、OpenAI Responses、Claude Messages、Gemini 四种协议。
- 页面只显示当前线路的一个 Key 输入框。切换保留草稿，保存配置时一并保存 Key；取消不写入。Key 不回显明文。
- 九项功能均使用当前线路。生图留空表示关闭；Claude 默认关闭生图。不会自动改用另一线路，也不会把生图失败替换为搜图。
- 官方模型可以从推荐列表选择，也可以自填模型 ID。具体模型的访问权限由用户的 API 账户决定。
- 模型默认值统一在 `core/model_settings.py`。新用户默认 Gemini，旧用户的模型 ID、空生图值及 Key 保持不变；旧 relay 在读取时兼容为 custom，保存时写入版本 2。
- 原 relay 的 Key 仍由 openrouter 凭证槽保存，避免迁移密钥。ChatGPT / Claude / Grok 分别使用 openai / anthropic / xai 槽。
- Key 只写入各用户加密的 `configs/credentials.json`，不会写入 `api_settings.json`。同一次保存失败会回滚模型配置和 Key。
- 本地离线模式继续禁止真实网络请求。单元测试模拟响应、浏览器回归使用合成账号；通过这些验证不等于真实供应商已连通。

## 当前默认值依据

Gemini 的聊天、翻译、识图、人设、通话、朋友圈、记忆、论坛按产品指定的模型配置；聊天与通话默认改为 `gemini-3.8-flash`，记忆改为 `gemini-3-flash-preview`（用户指定 Gemini 3.0 Flash 的官方预览 ID），生图改为 `gemini-nano-banana-2.1`。Gemini 4 Hydrogen 归入 Flash，Carbon / Argon 归入 Pro（本站分类）。默认值更新不会自动覆盖已保存的用户选择。

其他线路提供可编辑预设。API 协议与模型目录参考：

- https://ai.google.dev/gemini-api/docs/models
- https://ai.google.dev/gemini-api/docs/changelog
- https://developers.openai.com/api/docs/guides/text
- https://developers.openai.com/api/docs/guides/image-generation
- https://platform.claude.com/docs/en/models/overview
- https://platform.claude.com/docs/en/build-with-claude/working-with-messages
- https://docs.x.ai/developers/models
- https://docs.x.ai/developers/model-capabilities/imagine

验证命令：`pytest tests/test_saki_model_settings.py tests/test_credential_security.py`（需设置隔离的 SAKI_DATA_ROOT 和 pytest basetemp）；`node tests/test_api_settings_frontend.cjs`（指向隔离本地测试服务）。

## 扩充模型目录（2026-09-23）

可选目录独立于默认值，维护在 `core/model_catalog.py`。官方能力定位用于分组；九项功能的推荐是 SakiGlobal 的产品匹配，不是供应商公布的角色扮演评分，也未做真实 API 速度实测。实际延迟还受输入长度、推理量和服务负载影响。

- ChatGPT：7 个文本/识图模型，2 个生图模型。GPT-6 Astra 偏能力、Sol 偏较强推理、Luna 偏效率；保留 GPT-5.4、5.4 Mini、5.4 Nano、5 Mini 供选择。Sunburst 偏画质，Flare 偏日常生成速度。[官方目录](https://developers.openai.com/api/docs/models/all)、[GPT-6 指南](https://developers.openai.com/api/docs/guides/latest-model)、[Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)、[Nano](https://developers.openai.com/api/docs/models/gpt-5.4-nano)。
- Claude：9 个文本/识图模型。Fable 5.1、Opus 5.5、Sonnet 5、Haiku 4.5 按官方延迟定位说明；另列仍在役的 Opus 5/4.8/4.7/4.6 和 Sonnet 4.6。生图继续关闭。[官方对比](https://platform.claude.com/docs/en/models/overview)、[在役状态](https://platform.claude.com/docs/en/about-claude/model-deprecations)。
- Grok：4 个文本/识图模型，1 个生图模型。4.7 为旗舰；4.3 偏速度与成本；4.6、4.5 是其他在役选择，不宣称它们比 4.7 更快或更便宜。生图使用 Imagine Image 2.0。[官方目录](https://docs.x.ai/developers/models)、[4.3](https://docs.x.ai/developers/models/grok-4.3)、[4.5](https://docs.x.ai/developers/models/grok-4.5)、[4.6](https://docs.x.ai/developers/models/grok-4.6)。
- 已退役的 Grok 4/4.1 Fast 等型号不加入新目录；旧配置中的自填 ID 仍保留。[官方退役说明](https://docs.x.ai/developers/migration/may-15-retirement)。

| 功能 | ChatGPT 推荐 | Claude 推荐 | Grok 推荐 |
| --- | --- | --- | --- |
| 聊天 | gpt-6-sol | claude-sonnet-5 | grok-4.7 |
| 翻译 | gpt-6-luna | claude-haiku-4-5-20251001 | grok-4.3 |
| 识图 | gpt-6-astra | claude-sonnet-5 | grok-4.7 |
| 人设 | gpt-6-astra | claude-opus-5-5 | grok-4.7 |
| 语音通话 | gpt-6-luna | claude-sonnet-5 | grok-4.3 |
| 朋友圈 | gpt-6-luna | claude-sonnet-5 | grok-4.3 |
| 记忆 | gpt-6-luna | claude-sonnet-5 | grok-4.3 |
| 生图 | gpt-image-2.5-sunburst | 关闭 | grok-imagine-image-2.0 |
| 论坛 | gpt-6-luna | claude-sonnet-5 | grok-4.3 |

通话栏配置的是现有通话流程中的文字回复模型；不把 Realtime、TTS 等不同协议的专用模型混进文字模型列表。选型说明集中在顶部“如何选择模型？”，下拉选项保留能力分组与推荐标记；各功能卡不重复显示长说明，中英日一致。展开面板随内容自然撑开，不设固定最大高度。已有模型选择和自填 ID 不被覆盖；新默认仅用于未配置项和用户点击“恢复推荐值”。目录不代表当前 Key 的访问权限。

## 每日生图限额

- 默认每个账号每天 10 张，可在「线路与模型设置 → 生图 → 每日生图上限」填写正整数；和模型、Key 一起保存，取消则丢弃草稿。
- 所有线路共用账号的限额，切换线路、模型或 Key 不清空当天用量。用户 1 和其他管理员没有免限额特权；生图模型留空仍表示关闭。
- 使用 `DATA_ROOT/configs/image_usage.db` 的 SQLite 事务先占名额，成功保存并取得图片 URL 后累计，失败释放。并发请求不会超过剩余名额；重复完成同一请求不会重复计数。
- 旧 `users/<id>/logs/daily_gen_status.json` 的当天记录首次使用时纳入计数。每日沿用服务器日期重置，与 UI 语言无关；跨午夜完成的请求算在开始当天。
- 调低上限不会删除已用量；用量达到新上限后暂停当天后续生图。进程意外终止时，未完成名额保守地保留到当日结束，避免重启导致超额；普通失败会立即释放。

## 管理员与熔断

管理员仅由服务端 `SAKI_ADMIN_USER_IDS` 配置，格式如 `1` 或 `1,7`。本地现有 `.env` 已显式保留 `1`；模板留空，未配置时不授予任何账号管理员权限。部署/迁移时需显式填入实际管理员 ID，修改 `.env` 后重启服务。后台保留中英日，跟随登录账号的用户语言。

模拟登录保存真实发起的管理员 ID，15 分钟到期或手动退出后恢复；禁止模拟管理员账号，模拟期间不能使用后台管理接口。原管理员被移出名单、账号被删除或认证版本变更时终止模拟登录。

旧 relay 的 Cloudflare 拦截全局暂停已删除。个人熔断继续按“用户＋线路”隔离，错误两次冷却 10 分钟，三次禁用对应线路，不影响其他账号。旧 relay 配置迁移为自填写的兼容逻辑仍保留。
