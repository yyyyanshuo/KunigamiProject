# SakiGlobal 独立环境配置审计与填写清单
> 最新调整：`.env` 已改成可直接填写的简表，已补入已知默认值和独立 VAPID 密钥对。下文“8 项有效赋值/注释占位”是上轮审计记录，不代表当前数量。公共 Sakura 助手和角色广场试聊现在只读取 `SAKI_SERVICE_GEMINI_API_KEY`，不再读用户 1 的 Key；普通用户 Key 仍在界面填写。此新变量是原始 Gemini API Key，共用 GEMINI_BASE_URL；缺失时真实公共服务返回 503，不回退其他 Key。
> 2026-09-23 更新：线路与模型设置已支持四家官方 API 和自定义中转，Key 已移入该模块；生图使用当前线路。本页下方为原审计快照，新的配置与迁移方式以 [线路与模型设置说明](MODEL_SETTINGS_ZH.md) 为准。


审计日期：2026-09-21。范围是当前 SakiGlobal 的有效代码，不把历史注释、旧版演示和测试框架配置列作网站必填项。**目前仅能确认本地离线启动与模拟测试；真实第三方服务未配置验证。** 本次没有读取旧项目 `.env`，没有复制旧站账号、存储或密钥，也没有联网调用模型/存储/邮件/推送。

## 你现在要填什么

当前 `.env` 的 8 个已有有效赋值全部保留，只补了 45 个注释占位（不改变缺省行为）。检查只输出变量名与状态，不输出值。当前本地配置格式检查通过，但这不验证密钥与已有加密文件是否匹配，也不验证任何第三方账号权限。

| 类别 | 变量/设置 | 当前需要做什么 |
| --- | --- | --- |
| 本地启动必需 | `SAKI_LOCAL_TEST=true`、`SAKI_DATA_ROOT=.local-data/global`、`FLASK_SECRET_KEY` | 已有有效配置，当前无需重新填；不要关闭离线模式来绕过检查 |
| 本地推荐显式 | `KUNIGAMI_ENV=development`、`SAKI_PORT=5001` | 已有；仅在端口冲突时换本机空闲端口 |
| 独立环境自动生成 | `FLASK_SECRET_KEY`、`CREDENTIAL_ENCRYPTION_KEYS` | 已存在且保留。第一次从无 `.env` 创建新环境时 `init` 生成；已有环境不自动修补或轮换密钥 |
| 合成账号初始化 | `SAKI_TEST_EMAIL`、`SAKI_TEST_PASSWORD` | 已存在；仅 init 创建账号需要，run 不依赖这些值登录。密码变化不重置数据库账号 |
| 第三方测试凭据 | Gemini/relay、COS、邮件、STT、搜索、生图、音乐 | 可以全部以后填；当前没有验证。不要填旧站 Key/桶/邮箱/收件人 |
| 用户界面配置 | 用户 Gemini、relay、ElevenLabs Key；线路、模型、角色 Voice ID | 真实功能测试时在个人设置/角色设置中填写，见下文，不要误当作全都必须写入 `.env` |
| 部署配置 | 生产模式、正式密钥、公开站点 origin、独立服务账号/配额/代理 | 正式部署前另做验收；本任务没有部署，没有操作 DNS |

重新检查：

```bash
cd /mnt/d/program/KunigamiChat/SakiGlobal
.venv/bin/python saki_local.py doctor
```

`doctor` 会列出模拟、替身、禁用状态和未填写的变量名，**present 只代表有值，不代表真实服务可用**。必填/格式错误会返回非零退出码并给出变量名和修复说明。正常启动命令仍为 `.venv/bin/python saki_local.py run`，地址 `http://127.0.0.1:5001`；本次验收进程会停止。5001 已有进程仍返回旧版本提示，本轮不停止该进程；新代码临时在 5002 验证，未改 .env 的端口。要让 5001 上的实例使用新配置检查，请在原启动终端 Ctrl+C 后重启。

## 可选服务逐项取得方式

| 功能 | 填写位置/变量 | 获取方式与当前限制 |
| --- | --- | --- |
| Gemini 聊天 | 优先界面用户 Key；服务器回退 `GEMINI_API_KEY`；可选 `GEMINI_BASE_URL` | [Google 官方 API Key 说明](https://ai.google.dev/gemini-api/docs/api-key)，从 AI Studio 的独立项目取得。URL 默认官方基址，代码追加 `/v1beta/models/...`；不是 OpenAI 的 `/v1` 格式 |
| relay 聊天 | 界面用户 Key + API 线路/模型，或 `OPENROUTER_KEY` 与对应 URL 槽 | 从自行选择的兼容服务商取得匹配的 Key/基址；程序追加 `/chat/completions`。`old`/`new` 是现有线路槽名称，不表示授权连接旧站。无配置时现有线路选择 old；可在界面明确选 custom。两个历史商业地址已去掉，不再静默回退 |
| 语音识别 STT | `ELEVENLABS_API_KEY` | [ElevenLabs API Keys](https://elevenlabs.io/docs/overview/administration/workspaces/api-keys)，独立工作区/测试权限；[STT 说明](https://elevenlabs.io/docs/eleven-api/guides/cookbooks/speech-to-text)。服务器提供识别配额，不回退用户 TTS Key |
| 角色语音合成/克隆 | 界面用户 ElevenLabs Key + 角色 `voice_id` | 同一平台自己的测试账户；`voice_id` 来自对应可用声线。代码故意不使用服务器 STT Key 替代用户 TTS Key |
| 图片生成 | `SILICONFLOW_API_KEY`，或用户 Gemini Key/图片模型 | [SiliconFlow 官方接入说明](https://docs.siliconflow.cn/docs/userguide/quickstart)；现有 SiliconFlow 域名硬编码为 `.cn`，不能假定国际账户 Key 能使用。模型在界面配置，不是新增环境开关 |
| 图片/网页搜索 | `SERPER_API_KEY` | Serper 开发者账户 Key；请求头 `X-API-KEY`；不填则搜索不可用 |
| 对象存储 | `COS_SECRET_ID`、`COS_SECRET_KEY`、`COS_REGION`、`COS_BUCKET` | 从腾讯云 CAM 取得仅访问独立测试桶的身份，并在该桶控制台核对 SDK 地域 ID 和含 APPID 的完整桶名。不得填写原桶。当前使用本地上传替身，没有验证 COS |
| 邮件/找回密码 | `MAIL_SENDER`、`MAIL_PASSWORD`、`MAIL_SERVER`；`MAIL_PORT` 可省略为 465；`MAIL_RECEIVER` 是通知备用收件人 | 从自己的测试 SMTP 服务获取。代码仅使用 SMTP_SSL，不支持 STARTTLS 配置。QQ SMTP 隐式默认已删除；不能用虚构密码或真实用户收件人来验收 |
| 浏览器推送 | `VAPID_PRIVATE_KEY`、`VAPID_PUBLIC_KEY` | 后续用已安装 py_vapid 生成独立 P-256 密钥对。私钥格式由 pywebpush/py_vapid 实际读取方式决定，详见模板。公钥是前端可解码的 URL-safe base64。现有 VAPID_CLAIMS 联系邮箱硬编码在 app.py/auth.py，并没有 VAPID_SUBJECT 环境变量；正式启用前须另行改为自己的联系方式 |
| 网易云音乐开放接口 | `NCM_APP_ID`、`NCM_PRIVATE_KEY` | 申请网易云开放平台应用，登记自己的配对 RSA 公钥。代码要求未加密 PKCS#8 私钥/主体，并用 RSA-SHA256 签名，不能拿普通用户 ID、Cookie 或任意 token 代替 |
| 外部模型读取图片 | `PUBLIC_BASE_URL`，兼容别名 `SITE_URL` | 独立测试环境可达的 origin；默认取当前请求/代理头。正式部署才通常需要显式填写；不是购买了域名就已经接通。程序当前只保留 origin，不能靠路径前缀挂子站 |

现在不用去注册上述所有服务。`SAKI_LOCAL_TEST=true` 下即使填 Key 也不会自动启用；真实验证需要后续明确选定功能、独立账户、可控预算和离线策略，不能仅删除保护开关称为配置完成。

## 实际调用、数据和启动路径

- `saki_local.py` 是本地入口；`app.py` 直接执行和 Flask/W​​SGI 导入也先读 `core.runtime`。只显式加载当前源代码根目录的 `.env`，`override=False`，进程变量优先；不依赖 cwd，不向父目录搜索。`.env` 中 `${...}` 插值由 python-dotenv 处理，不应引用旧项目变量。
- 默认本地模式现在为 true；缺少独立数据路径/会话密钥会报变量名，不再以旧目录或临时会话密钥悄悄启动。只有明确关闭本地模式时才保留旧路径兼容分支；这不是本地推荐入口。
- 没有数据库连接串环境变量：SQLite、账号、聊天、配置、媒体、日志、缓存由 `SAKI_DATA_ROOT` 派生；`BASE_DIR` 仍是代码资源根目录。没有 Redis/数据库服务器配置，也没有需要填的生产数据库 URL。
- `memory_jobs.py` 的每日/每周总结、日记、主动消息和朋友圈任务最终复用同一套用户资料、AI helper、邮件和推送配置，没有额外任务凭据。当前树没有调用 `BackgroundScheduler.start()` 的有效入口，没有后台任务环境开关。不能把旧服务器的三个服务推断成此处已配置的服务。
- 多媒体有直连 HTTP 路径，模型共用 helper 才会返回 MOCK；论坛流式生成本地明确禁用。其余真实外发受本地网络保护拦截，不把这些功能记作 MOCK 验证成功。
- 本地真实邮件/推送、STT/TTS、音乐网络接口、天气/地理编码会返回 503 与功能名/相关配置名；COS 使用明确的本地替身；常规聊天 helper 返回带 MOCK 标识的回复。`/api/local/config-status` 登录后可查看状态，无密钥值。
- 身份优先级：普通聊天先读取当前用户加密 Key（保留既有明文兼容读取），再用服务器 `.env` Key；公共 Sakura 和角色广场试玩只使用 `.env` 的 `SAKI_SERVICE_GEMINI_API_KEY`，不再绑定任何用户凭据。开真实功能前须确认由哪个测试账户付费，不能默认所有功能都用当前访客的 Key。
- 会话 Cookie 独立为 `saki_global_session`；保存的第三方凭据使用 Fernet。`init` 在 `.env` 已存在时保持原文件，不补发新密钥；缺失已使用的加密密钥只提示恢复，不生成替代物。

## 审计范围与动态读取

运行时扫描覆盖 app/core/blueprints/services、音乐/天气、启动器和被引用的管理代码；手工脚本单列。发现 50 个网站/本地启动变量、3 个手工脚本凭据变量。以下不是需要照搬的网站配置：

- `_positive_timeout_from_env(name, default)` 的动态调用实际只有 `GEMINI_CONNECT_TIMEOUT_SECONDS` 与 `GEMINI_READ_TIMEOUT_SECONDS`。
- `os.getenv(args.password_env)` 来自手工测试脚本的 `--password-env`，默认 `KUNIGAMI_ASSESSMENT_PASSWORD`；使用自定义名字时由操作者自己设置。翻译脚本复用同一 `make_session`。`KUNIGAMI_ASSESSMENT_REEXEC=1` 是进程内部防循环标记，不是用户配置。
- 手工脚本的 raw Cookie 分支硬编码 `session`，不适用于本地 `saki_global_session`；不复制旧站 Cookie。这些手工脚本在当前本地进程的网络保护下不能进行 HTTP 联调，不作为本地启动或验收必需工具。
- 未被调用的 `_load_sakura_gemini_key` 旧 helper 已删除；实际 Sakura API 现通过 call_gemini(service_key=True) 使用独立服务 Key。
- `USE_OPENROUTER` 只有定义/导入，没有生效条件分支，本次已删除无效读取。真实路由由用户 API 设置决定，不把废弃开关写入模板。
- `Voice/` 是未被主应用导入的独立演示，当前主站语音实现位于 `services/elevenlabs.py` 和 blueprints。`agent-test/` 是独立实验工具，其 `KUNIGAMI_ROOT` 甚至默认历史服务器绝对路径，不能当作本次启动入口；没有运行这些工具。测试脚本的 `KUNIGAMI_PLAYWRIGHT_MODULE`/`KUNIGAMI_BROWSER_EXECUTABLE` 等属于验收工具，不属于网站配置。
- Python/依赖层还可能读取 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`（含小写）、`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`、`SSL_CERT_FILE`/`SSL_CERT_DIR`、`PYTHONDONTWRITEBYTECODE`、`PYTHON_DOTENV_DISABLED` 等。它们不是应用服务凭据，不从旧 `.env` 搬入；代理/CA 仅在后续网络联调需要时按本机环境配置。python-dotenv 的禁用标志会导致 `.env` 未加载，应清除该进程标志再启动。系统 PATH/HOME、第三方库内部调试参数不作为此应用的可填写业务配置。
- 历史公告时间仍由有效 API 读取，因此列入模板，但它们仅控制公告显示，既不启用语音也不触发旧数据删除。

## 按变量的代码依据

模板中的格式来自这些读取点及其消费者；不是依据变量名推测。详细用途/格式/必填/缺失影响/获取方式全部写在 `.env.example` 相邻注释中。

| 变量 | 有效读取/解析依据 |
| --- | --- |
| `KUNIGAMI_ENV` | `app.py:223`、`core/runtime.py:21` |
| `FLASK_ENV` | `app.py:223`、`core/runtime.py:21` |
| `SAKI_LOCAL_TEST` | `core/runtime.py:20` |
| `SAKI_DATA_ROOT` | `app.py:342`、`app.py:1127`、`core/config.py:24`、`core/runtime.py:24` |
| `SAKI_PORT` | `app.py:6017`、`saki_local.py:106`、`saki_local.py:119` |
| `FLASK_SECRET_KEY` | `app.py:261`、`core/environment_check.py:configuration_errors` |
| `CREDENTIAL_ENCRYPTION_KEYS` | `core/credentials.py:71`、`core/environment_check.py:configuration_errors` |
| `SAKI_TEST_EMAIL` | `saki_local.py:47` |
| `SAKI_TEST_PASSWORD` | `saki_local.py:48` |
| `SESSION_LIFETIME_DAYS` | `app.py:277` |
| `FLASK_DEBUG` | `app.py:6016` |
| `ALLOW_LEGACY_PLAINTEXT_LOGIN` | `blueprints/auth.py:320` |
| `GEMINI_API_KEY` | `app.py:197`、`core/config.py:7`、`core/config.py:339` |
| `GEMINI_BASE_URL` | `app.py:1487`、`services/ai_client.py:481`、`blueprints/forum.py:223`、`blueprints/media.py:1667` |
| `OPENROUTER_KEY` | `app.py:198`、`core/config.py:8` |
| `OPENROUTER_BASE_URL` | `app.py:200`、`core/config.py:9` |
| `OPENROUTER_BASE_URL_OLD` | `app.py:202`、`core/config.py:10` |
| `GEMINI_CONNECT_TIMEOUT_SECONDS` | `services/ai_client.py:37` |
| `GEMINI_READ_TIMEOUT_SECONDS` | `services/ai_client.py:40` |
| `ELEVENLABS_API_KEY` | `core/config.py:14` |
| `SILICONFLOW_API_KEY` | `app.py:205`、`core/config.py:12` |
| `SERPER_API_KEY` | `app.py:206`、`core/config.py:13` |
| `COS_SECRET_ID` | `cos_utils.py:12` |
| `COS_SECRET_KEY` | `cos_utils.py:13` |
| `COS_REGION` | `app.py:211`、`app.py:3148`、`app.py:3520`、`app.py:3630`、`app.py:3051`、`cos_utils.py:14`、`core/config.py:17`、`blueprints/chat.py:1689`、`blueprints/group.py:1404` |
| `COS_BUCKET` | `app.py:210`、`app.py:3147`、`app.py:3519`、`app.py:3629`、`app.py:3050`、`cos_utils.py:15`、`core/config.py:16`、`blueprints/chat.py:1688`、`blueprints/group.py:1403` |
| `MAIL_SENDER` | `app.py:4398`、`blueprints/auth.py:412` |
| `MAIL_PASSWORD` | `app.py:4399`、`blueprints/auth.py:413` |
| `MAIL_SERVER` | `app.py:4412`、`blueprints/auth.py:414` |
| `MAIL_PORT` | `app.py:4414`、`blueprints/auth.py:415` |
| `MAIL_RECEIVER` | `app.py:4406`、`app.py:4408` |
| `VAPID_PRIVATE_KEY` | `app.py:860`、`blueprints/auth.py:37` |
| `VAPID_PUBLIC_KEY` | `blueprints/auth.py:38` |
| `NCM_APP_ID` | `music_api.py:15` |
| `NCM_PRIVATE_KEY` | `music_api.py:16` |
| `PUBLIC_BASE_URL` | `blueprints/media.py:1637` |
| `SITE_URL` | `blueprints/media.py:1637` |
| `VOICE_STT_MAX_REQUESTS_PER_HOUR` | `blueprints/media.py:1176` |
| `VOICE_STT_MAX_REALTIME_SESSIONS_PER_HOUR` | `blueprints/media.py:1179` |
| `VOICE_CALL_STT_SESSIONS_PER_HOUR` | `blueprints/calls.py:788` |
| `VOICE_CALL_BATCH_STT_PER_HOUR` | `blueprints/calls.py:858` |
| `VOICE_CALL_STT_MAX_BYTES` | `blueprints/calls.py:852` |
| `SQUARE_TRIAL_RATE_LIMIT_PER_MINUTE` | `blueprints/square.py:31` |
| `SQUARE_TRIAL_PENDING_TIMEOUT_SECONDS` | `blueprints/square.py:34` |
| `SAKI_SERVICE_GEMINI_API_KEY` | `services/ai_client.py:call_gemini`、`blueprints/square.py:api_square_trial_chat` |
| `SQUARE_TRIAL_GEMINI_MODEL` | `blueprints/square.py:1074` |
| `VOICE_ANNOUNCEMENT_START` | `blueprints/views.py:13` |
| `VOICE_ANNOUNCEMENT_END` | `blueprints/views.py:17` |
| `VOICE_LEGACY_DELETE_AT` | `blueprints/views.py:21` |
| `PERSONA_LOCK_ANNOUNCEMENT_START` | `blueprints/views.py:27` |
| `PERSONA_LOCK_ANNOUNCEMENT_END` | `blueprints/views.py:31` |
| `KUNIGAMI_ASSESSMENT_USERNAME` | `scripts/run_character_ego_test.py:372` |
| `KUNIGAMI_ASSESSMENT_PASSWORD` | `scripts/run_character_ego_test.py:375` |
| `KUNIGAMI_ASSESSMENT_SESSION_COOKIE` | `scripts/run_character_ego_test.py:364` |


## 验证层级

1. **程序能启动**：本地格式检查、独立路径、无真实凭据导入及登录页面启动可验证；不代表第三方配置正确。
2. **模拟测试通过**：合成账号、角色、MOCK 消息持久化和目录隔离可验证；固定 MOCK 不是模型供应商响应。
3. **真实服务验证通过**：需要明确选择的测试账户、凭据、权限与端点，实际调用并核对结果/费用。本次此项为 **未验证**。

`.env`、虚拟环境、数据根目录、日志和私钥文件已在 Git 忽略规则内；模板/本清单可提交。没有暂存、提交或推送。本次检查不输出值，且在补充注释占位前后以程序断言确认 `.env` 的所有原有效赋值保持相同。

### 本轮验证结果

- 6 项配置专项测试通过：模板覆盖（含动态超时读取）、缺失/格式错误值不泄露、已有加密记录缺密钥不生成替代物、离线功能返回明确 503、缺模型 Key 不发请求、无历史 relay/SMTP 隐式地址。
- 3 组目录/账号/模拟聊天隔离测试通过。
- 28 项既有配置/凭据测试在它们要求的旧路径兼容模式下通过，仅凭据测试使用临时合成文件；没有导入旧项目。第一次把隔离根目录注入旧路径断言时有两项环境假设冲突，移除该测试进程的数据根覆盖后通过，未修改测试断言掩盖差异。
- 新代码从 `/tmp` 启动，临时端口 5002 的 `/login` 返回 HTTP 200；未配置邮件的请求明确返回 `local_feature_unavailable` 和配置变量名，未发邮件。该验收进程已停止。
- 5001 上先前存在的旧进程未动；其旧响应不算本轮新代码验收。请在其原终端停止并重新启动以应用改动。
- `git diff --check` 通过；有效 `.env` 赋值仍为原来的 8 项；可选第三方凭据实际有效赋值为 0；Git 暂存区为空。
- 本轮没有真实第三方服务调用验收；没有把格式校验、模拟回复或 HTTP 200 当作真实服务验证。


## 管理员配置更新

`SAKI_ADMIN_USER_IDS` 使用英文逗号分隔的正整数用户 ID。未配置或留空表示没有管理员，不再自动把用户 1 当管理员。本项目本地 `.env` 已显式保留 `SAKI_ADMIN_USER_IDS=1`；部署到其他环境时须明确配置实际管理员 ID 并重启服务。此配置不通过用户资料或前端接口修改。

管理员不享有生图免限额。生图现在默认每个账号每天 10 张，可在个人主页的生图设置中修改；所有线路共用当天用量。详情见 [线路与模型设置](MODEL_SETTINGS_ZH.md)。
