# SakiGlobal 本地独立测试环境（WSL）
> 配置完整性以 [配置审计与填写清单](CONFIGURATION_CHECKLIST_ZH.md) 和 `.env.example` 为准。本文的“初始化/模拟验收完成”不代表第三方服务已配置或已通过真实调用验证。

本目录是 `codex/global-beta` worktree，保留现有页面和业务代码。本次提供本地离线测试环境，不代表海外版 UI、品牌、合规文本或真实云服务已经改造完成。未操作服务器、域名、原项目用户数据或密钥。

## 启动、停止、重启

在 **WSL 的 Bash 终端**执行（Windows 浏览器可以打开网址）：

```bash
cd /mnt/d/program/KunigamiChat/SakiGlobal
.venv/bin/python saki_local.py run
```

浏览器打开 `http://127.0.0.1:5001`。若端口被其他程序占用，在本目录 `.env` 修改 `SAKI_PORT`；首次初始化会检查 5001–5099 并记录可用端口。查看实际地址：

```bash
.venv/bin/python saki_local.py doctor
```

前台终端 `Ctrl+C` 停止，再执行启动命令即重启。单进程、关闭 debug 和自动重载，不启动独立调度器。不要同时运行多个服务进程。

`.venv` 是 Linux Python 3.12 环境，不能在 PowerShell 中当作 Windows 虚拟环境运行。Windows 可先打开 WSL，再执行以上命令。也可从任意 WSL 工作目录运行这两个绝对路径：

```bash
/mnt/d/program/KunigamiChat/SakiGlobal/.venv/bin/python /mnt/d/program/KunigamiChat/SakiGlobal/saki_local.py run
```

## 配置与测试账号

初始化命令（可以重复执行）：

```bash
.venv/bin/python saki_local.py init
```

首次创建本目录 `.env`，生成随机会话密钥、独立 Fernet 凭据加密密钥和随机测试密码，创建新数据库、合成账号 `test@saki.invalid`、原创测试角色“小咲（本地测试）”。密码在 `.env` 的 `SAKI_TEST_PASSWORD`，不会打印到日志或提交 Git。已有账号密码、角色、聊天记录不会被覆盖；修改 `.env` 的测试密码不会重置已有账号。

公开注册页面和 API 已关闭。将现有测试账号的用户 ID 加入 `SAKI_ADMIN_USER_IDS` 后，在管理员数据看板的“新建账号”中创建其他本地账号，填写邮箱和可选昵称即可；24 位随机初始密码仅在创建成功后显示，请及时复制并私下交给用户。用户登录后可在个人主页修改密码、确认协议。仅使用合成邮箱、测试资料。忘记密码邮件在离线模式禁用。

配置优先级：**进程显式环境变量 > 本项目 `.env` > 代码默认值**。不搜索当前工作目录或父目录的 `.env`。`SAKI_DATA_ROOT` 相对路径以代码根目录解析，与启动目录无关。本地模式只允许本项目 `.local-data/` 内的数据根目录，自动测试允许 `/tmp/saki-*`。

默认不需要填写任何真实服务配置。只测试界面、角色、模拟聊天和持久化时，保持现有 `.env` 即可。端口冲突时只需修改 `SAKI_PORT`。密钥必须保留，删除会导致会话失效或测试凭据无法解密。

未来若要验证真实模型、语音、存储或邮件，需要另行配置测试用途的 API 路由/模型及 Gemini 或 relay key、独立测试 COS 桶/权限、测试 SMTP/推送凭据，并调整离线策略。本次没有启用这些服务；即使填入密钥，`SAKI_LOCAL_TEST=true` 仍禁止外发。不要通过简单关闭本地模式连接原项目资源。

## 数据与资源

所有运行数据位于 `.local-data/global/`（或明确配置的本地数据根目录）：

| 子路径 | 内容 |
| --- | --- |
| `configs/` | 新账号 SQLite、广场/论坛、设备账号、全局测试配置、天气地理缓存 |
| `users/<id>/` | 用户配置、加密凭据、角色与聊天 SQLite、记忆、群聊、朋友圈、语音、音乐、上传图片、用户日志 |
| `characters/`、`groups/`、`stickers/` | 本地全局测试空间，不拷贝旧项目内容 |
| `static/` | 运行时广场头像及图片处理临时副本 |
| `logs/local.log` | 启动器与 Flask 日志，轮转保留；部分旧代码 print 输出在终端 |
| `tmp/` | Python 临时文件、运行缓存 |
| `instance/` | 未显式填写会话密钥时的开发密钥回退位置 |

`BASE_DIR` 仍表示代码目录，模板、静态资源和只读问卷继续从源码加载。应用中重复定义的账号、角色、论坛、天气、音乐及模型日志路径均改为数据根目录。配置了新数据根目录就不执行旧账号迁移；本地新账号也不复制全局配置快照。未启用新配置时保留原数据路径行为。

上传使用当前账号专属 `users/<id>/uploads/`，返回 `/local_uploads/<filename>`，读取需要登录。会话 Cookie 为 `saki_global_session`，设备 Cookie 为 `saki_global_device_id`，避免与旧站端口共用名称。

## 离线边界

- Gemini 和 relay 调用入口返回明确标注 `MOCK` 的固定回复，经过现有聊天接口保存；不作为真实模型成功证据。
- 不启动调度器；聊天前自动总结、邮件与推送通知在本地禁用。其他需要外部服务的功能可能返回缺配置或离线错误。
- 本地 COS 客户端不初始化；上传使用本地替身。网络连接、外部 DNS、子进程被运行时审计钩子拦截。
- 审计钩子检查文件写入和 SQLite 路径，阻止数据根目录外写入。它是开发保护措施，不是运行不可信 Python 的安全沙箱。
- 浏览器 CSP 限制站外连接/资源；外部 CDN 的截图导出库、外部图片等在此模式下不可用。页面已有品牌和文案没有全面改动。
- 生产环境与 `SAKI_LOCAL_TEST=true` 同时设置时启动拒绝，不允许模拟模式意外用于生产。

## 依赖与验证

生产基线为 Linux x86_64、CPython 3.12.3、Node 22.22.2 和 npm 10.9.7。Python 运行依赖及传递依赖固定在 `requirements.txt`；开发环境用 `requirements-test.txt` 额外安装固定版本的 pytest 和测试依赖。以下命令用于新建或空的虚拟环境。Ubuntu 需要 Python 3.12 的 venv 模块；部分包从源码构建，需要 C 编译器及 Python 开发头文件。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes --index-url https://pypi.org/simple -r requirements-test.txt
python -m pip check
npm ci --prefix agent-test --ignore-scripts --no-audit --no-fund
npm test
```

`npm test` 在仓库根目录运行，依次执行 Python 核心回归和 Agent 的模拟页面测试。仅运行 Python 时可用 `.venv/bin/python scripts/check_core.py`。入口在导入应用前禁用项目 dotenv，使用独立的临时数据目录和合成账号，检查账号隔离、凭据与线路、记忆编辑、论坛、地图、调度及注销。默认 pytest 收集会在离线运行时尚未就绪时退出。回归不需要线上配置或浏览器二进制。Agent 实际启动还需要 Playwright 配套的 Chromium，见 [Agent 部署说明](AGENT_DEPLOYMENT_ZH.md)。

新增隔离测试（不会导入真实旧站目录）：

```bash
.venv/bin/python -m unittest discover -s tests -p test_saki_local.py -v
```

测试以子进程在 `/tmp` 启动，使用两个临时根目录与重叠用户 ID，覆盖未登录拦截、错误密码、正常登录、主页/聊天页面、角色创建、模拟回复、历史落库与新进程读取、账号图片上传与读取、禁止外发和越界写入、生产模式拒绝与旧路径兼容。

不要直接对本地已有测试数据运行整个历史测试套件；它包含旧式模块补丁和数据库假设。若跑全套，请使用独立临时副本/数据目录并确保外发被阻止。

`.env`、`.venv/`、`.local-data/`、日志与数据库均已忽略；`.env.example`、本文件、启动器与自动测试可被 Git 跟踪。本次不提交、不推送、不合并。

## 改动文件

- 新增：`.env.example`、`saki_local.py`、`core/runtime.py`、`core/local_storage.py`、`tests/test_saki_local.py`、`tests/saki_local_browser.cjs`、本文。
- 配置与入口：`.gitignore`、`core/config.py`、`app.py`、`cos_utils.py`、`music_api.py`。
- 运行路径：`core/utils.py`、`admin_stats.py`、`music_manager.py`、`weather_api.py`；`services/ai_client.py`、`services/prompt_builder.py`、`services/character_assessment.py`；`blueprints/auth.py`、`forum.py`、`group.py`、`media.py`、`moments.py`、`views.py`。
- 页面：`templates/chat.html`、`templates/square/trial_chat.html` 在本地模式下不加载外部截图 CDN，避免诊断弹层遮挡聊天。
- 本机生成但忽略：`.env`、`.venv/`、`.local-data/`。不包含原项目数据副本。


## 本次实际验收（2026-09-21）

- 3 组新增隔离测试通过，覆盖两根目录/重叠 ID、账号隔离、重启读历史、上传读取、离线与路径保护、生产拒绝、兼容旧路径。
- 13 项既有凭据安全和前端会话恢复测试通过（临时测试路径，不对真实数据运行迁移）。
- Chromium 实际浏览器：登录、发送合成消息、收到 MOCK、刷新后读取历史、独立 Cookie 通过。
- 现有 API 设置页浏览器回归通过。
- 从 `/tmp` 使用绝对路径启动，`/login` HTTP 200，浏览器可访问 `http://127.0.0.1:5001`。
- `pip check` 和 `git diff --check` 通过；Git 暂存区为空，环境/数据库/日志未被跟踪。
- 没有进行真实模型、COS、邮件、推送或完整历史测试套件验收。验收服务已停止，按“启动”一节自行运行。

浏览器回归脚本为 `tests/saki_local_browser.cjs`，需要 Node、Playwright 与 Chromium；若本机通过 `npm ci` 安装前端依赖，可在服务运行时执行 `node tests/saki_local_browser.cjs`。也支持 `KUNIGAMI_PLAYWRIGHT_MODULE`、`KUNIGAMI_BROWSER_EXECUTABLE` 指定现有测试运行时，本次复用了本机现有 Windows 运行时。正常启动网页不需要 Node。

本轮配置审计增加 `core/environment_check.py`、`tests/test_environment_configuration.py` 与 `CONFIGURATION_CHECKLIST_ZH.md`；必填项缺失会报变量名，原有密钥不自动替换，历史 relay/SMTP 默认地址已移除。
