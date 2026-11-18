# CHG-20261118-001：Gemini 默认模型与 Gemini 4 分组

- 日期：2026-11-18（Asia/Shanghai）
- 状态：已发布、已验收
- 需求：在 2026-11-30 23:59 前更新默认模型，新增默认生图模型与 Gemini 4；用户后续更正记忆目标为 Gemini 3.0 Flash。

## 为什么改

避免继续推荐即将于 12 月 1 日退役的 Gemini 2.5 Pro / Flash，并提供用户指定的生图模型及 Gemini 4 选择。

## 改了什么

| 场景 | 修改前 | 修改后 |
|---|---|---|
| 聊天、通话默认 | gemini-2.5-pro | gemini-3.8-flash |
| 记忆默认 | gemini-2.5-flash | gemini-3-flash-preview（官方 Gemini 3.0 Flash ID，预览版） |
| 生图默认 | gemini-3-pro-image | gemini-nano-banana-2.1 |
| Gemini 4 选项 | 无 | gemini-4-hydrogen 归入 Flash；gemini-4-carbon、gemini-4-argon 归入 Pro |

Gemini 4 分组是本站用途分类，不是 Google 官方命名。保留旧生图备选。其他任务默认保持原值。自填写线路沿用既有文本默认继承方式，生图仍默认关闭。

## 影响与兼容

变更模型配置 API 推荐值、未配置任务的默认值与前端选项。已保存的用户模型 ID（包括旧 2.5 模型）、自定义 ID 和显式关闭生图保留，不写入或批量迁移用户文件。用户可点“恢复推荐值”并保存应用新默认值。取消编辑不生效；权限、密钥和模型调用协议不变。普通配置调整，无需另立 ADR。

## 官方核对（2026-11-18）

- [模型列表](https://aistudio.google.com/docs/models)：3.8 Flash、Nano Banana 2.1、Gemini 4 三款的准确 ID。
- [Gemini 3 Flash Preview](https://aistudio.google.com/docs/models/gemini-3-flash-preview)：3.0 Flash 使用 gemini-3-flash-preview。
- [退役说明](https://aistudio.google.com/docs/deprecations)：2.5 Pro / Flash 12 月 1 日退役。最初提及的 2.0 Flash 已退役；用户已更正为 3.0 Flash，不采用官方其他替代建议。

## 验收与结果

- 三语指南隔离渲染：`.venv/bin/python -m unittest discover -s tests -p test_saki_guide.py -v`，通过。检查三语新模型内容、对应章节和锚点。
- 核心回归：`.venv/bin/python scripts/check_core.py`，158 passed、32 subtests passed；另有 2 个既有 TLS/SNI 用例失败（`test_public_https.py::test_real_tls_host_sni_no_proxy_credentials_or_redirect` 的两组参数），不涉及本次改动路径；核心检查未全通过，后续安全边界阶段未执行。
- 模型配置定向测试：沿用 `test_saki_local.child` 临时离线数据、预加载 `core.runtime` 并将 pytest 日志写入隔离数据根目录，47 passed。最初两次定向启动因未预加载 runtime、日志落点在隔离目录外被保护机制拒绝，调整启动参数后通过，未绕过保护。
- 增加默认值、Gemini 4 分类、保存旧选择及关闭生图兼容性回归；浏览器用例增加实际 optgroup 断言。
- 当前 shell 无 Node，未执行浏览器回归。未进行真实模型计费调用，不证明账号权限或供应商可用性。

## 文档与发布

同步 `templates/guide/zh-CN.html`、`ja.html`、`en.html` 的 `#models` 章节（模型选择、默认值、预览状态、恢复推荐值）；`MODEL_SETTINGS_ZH.md` 更新配置说明；`CHANGELOG.md` 未发布区有链接。

用户已授权按本地 sakiglobal-deploy 技能上线。发布批次 `saki-models-20261118`，状态：已发布、已验收。生产基线已核实；最小包保留线上独有翻译与更新日志。备份目标 `/var/backups/sakiglobal/saki-models-20261118`，验收后记录回执。

发布前复跑核心回归（获准执行环境）：160 passed、32 subtests passed，2 项隔离安全测试通过。此前 TLS/SNI 失败在此环境未复现，不修改 HTTPS 代码。


## 上线验收回执

北京时间 2026-11-18T15:07:15+08:00 完成发布和独立线上验收。详见 [批次回执](../../deploy/releases/saki-models-20261118/receipt.txt)、[清单](../../deploy/releases/saki-models-20261118/manifest.json)、[验收结果](../../deploy/releases/saki-models-20261118/verification.txt)。9 个生产文件与备份哈希通过，三个服务 active，公网三语指南语言及内容通过。未触发回滚，未执行真实模型调用。包内变更文档保留安装前状态快照；本记录和批次回执保存最终状态。
