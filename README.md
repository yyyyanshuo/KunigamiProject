# KunigamiProject 🌸

一个轻量级、重沉浸的多角色 AI 聊天应用。支持单聊/群聊、朋友圈、分级记忆系统（短期/中期/长期）、主动消息与表情包系统，致力于提供无尽历史记录与拟人化社交体验。

## 🏗️ 架构升级 — v3.0 模块化重构

本项目根目录的 `app.py` 已从单文件巨石（~15,000 行）重构为三层模块化架构：

```
KunigamiProject/
├── core/                         # 核心基础设施
│   ├── config.py                 #   全局配置、API 密钥、路径常量、系统规则
│   ├── context.py                #   用户上下文 (ContextVar)、后台任务身份管理
│   └── utils.py                  #   工具函数 (路径解析、注音、JSON、地图数据)
│
├── services/                     # AI 核心服务层
│   ├── ai_client.py              #   Gemini / OpenRouter 调用、模型路由、用量日志
│   ├── prompt_builder.py         #   System Prompt v2 构建、记忆提取、关键词分词
│   └── memory.py                 #   短期/中期/长期记忆摘要与更新
│
├── blueprints/                   # 业务路由 (9 个 Blueprint, 154 条路由)
│   ├── admin.py                  #   管理面板 (3 路由)
│   ├── auth.py                   #   登录 / 注册 / 账号切换 / VAPID 推送 (12)
│   ├── chat.py                   #   1v1 单聊核心 (26)
│   ├── group.py                  #   多角色群聊 (19)
│   ├── lbs.py                    #   地图 / 天气 / 地理编码 (13)
│   ├── media.py                  #   表情包 / 音乐播放 / TTS / Vision (38)
│   ├── moments.py                #   朋友圈动态 (19)
│   ├── square.py                 #   角色广场 (17)
│   └── views.py                  #   HTML 页面入口 (7)
│
├── app.py                        # Flask 入口 (系统/用户配置路由, 25 路由)
├── memory_jobs.py                # APScheduler 定时任务 (日结/周结/主动消息)
├── agent_utils.py                # AI Agent 动作标签解析
├── music_api.py / music_manager  # 网易云音乐集成
├── cos_utils.py                  # 腾讯云 COS 文件上传
├── weather_api.py                # 天气 / 地理编码
│
├── templates/                    # Jinja2 前端模板
├── static/                       # 静态资源 (CSS, JS, 图片)
├── stickers/                     # 官方表情包
│
├── configs/                      # 全局配置文件 (JSON/DB)
├── users/                        # 多用户隔离数据目录
│   └── <user_id>/
│       ├── characters/<char_id>/ # 角色数据 (chat.db, prompts/, 头像, 背景)
│       ├── groups/<group_id>/    # 群聊数据 (chat.db, prompts/)
│       └── configs/              # 用户级配置 (characters.json, user_settings.json)
│
├── docs/                         # 文档
└── requirements.txt              # Python 依赖
```

## ✨ 功能概览 (Features)

- **💬 多角色聊天系统**
  - 单聊与群聊无缝切换，基于独立 SQLite 数据库保存所有聊天历史。
  - 类似主流 IM 的体验：自动加载历史消息、多气泡连续发送、拟人化打字延迟、智能时间轴分隔。
  - **👋 拍一拍 (Tickle)**: 双击头像发送拍一拍，支持群聊与单聊，自带多语言提示与自定义后缀。
  - **🖼️ 表情包系统**: AI 可通过 `[表情]开心` 描述词发送表情，支持多套表情库、用户上传与"写时随机"机制。
  - **🎵 音乐播放器**: 聊天中可通过 `[音乐]曲名` 标签触发搜索与播放，支持播放列表管理。
  - **🗣️ 语音克隆 (TTS)**: 基于 ElevenLabs 的语音合成，点击消息即可朗读。

- **🧠 自动化分级记忆系统**
  - **短期记忆 (Short)**: 按天记录的事件级日志，对话与朋友圈互动后自动提取。
  - **中期记忆 (Medium)**: 后台定时任务自动日结，生成日记式总结。
  - **长期记忆 (Long)**: 自动周结/月结，结合 RAG（检索增强生成）在对话时精准提取。
  - **记忆面板**: 前端 UI 查看、手动编辑和 AI 润色各级记忆。
  - **跨场景同步**: 单聊前同步群聊记忆，群聊前同步各成员单聊记忆。

- **📱 朋友圈 (Moments)**
  - 角色可主动发布朋友圈（文字与 AI 生成图片）。
  - 用户可点赞、评论，角色根据性格智能回复。
  - `@角色名` 提及机制 & `[DIRECT_TO_GROUP]` 一键拉群。
  - 社交互动自动沉淀到短期记忆中。

- **🗺️ 地理位置与天气感知**
  - 地图页面的虚拟坐标系，支持添加/移动地点。
  - 角色与用户位置实时展示，地点感知自动注入 System Prompt。
  - 天气 API 接入，实时获取真实天气数据。

- **🏪 角色广场 (Square)**
  - 上传/下载/分享角色到公共广场。
  - 点赞、收藏、评论，AI 自动补全角色关系图谱。
  - 支持标签搜索和 IP (作品系列) 分类。

- **🔔 主动消息 (Active Messages)**
  - 基于情绪指数与作息时间，角色主动发送消息、邮件或 Web Push 通知。

- **🌐 多语言与灵活配置**
  - 中日英三语 AI 内核，全局切换沉浸式模式。
  - 兼容 OpenAI 格式 API (OpenRouter) 与 Google Gemini 原生 SDK。
  - 角色年龄基于生日自动 +1，与核心 Prompt 解耦。
  - 多中转商支持 (自定义 Base URL / 渠道切换)。

## 🛠️ 技术栈 (Tech Stack)

| 层级 | 技术 |
|------|------|
| **后端框架** | Python 3.x, Flask, APScheduler |
| **数据库** | SQLite3, JSON |
| **前端** | HTML5, CSS3, Vanilla JavaScript |
| **AI 引擎** | Google Gemini SDK / OpenRouter (兼容 OpenAI) |
| **多媒体** | ElevenLabs TTS, 网易云音乐 API, 腾讯云 COS |
| **搜索** | Serper API (Google 搜索) |
| **推送** | Web Push (VAPID) |
| **图像** | Pillow, SiliconFlow / Google Imagen |
| **分词** | jieba (中文), janome (日语), pykakasi (注音) |

## ⚙️ 配置指南 (Setup)

### .env 文件

在根目录创建 `.env` 文件：

```env
# === 必须配置 ===
# Google Gemini API
GEMINI_API_KEY=your_gemini_api_key

# OpenRouter (兼容 OpenAI)
USE_OPENROUTER=true
OPENROUTER_KEY=your_openrouter_api_key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_BASE_URL_OLD=https://vg.v1api.cc/v1

# === Web Push 推送 (可选) ===
VAPID_PUBLIC_KEY=your_vapid_public_key
VAPID_PRIVATE_KEY=your_vapid_private_key
VAPID_CLAIMS_EMAIL=mailto:your@email.com

# === 邮件通知 (可选) ===
MAIL_SENDER=your_email
MAIL_PASSWORD=your_smtp_password
MAIL_SERVER=smtp.qq.com
MAIL_PORT=465

# === 腾讯云 COS 对象存储 (可选) ===
COS_SECRET_ID=your_cos_secret_id
COS_SECRET_KEY=your_cos_secret_key
COS_REGION=ap-singapore
COS_BUCKET=your_bucket_name

# === 图像生成 (可选) ===
SILICONFLOW_API_KEY=your_siliconflow_key
SERPER_API_KEY=your_serper_key

# === ElevenLabs TTS (可选) ===
ELEVENLABS_API_KEY=your_elevenlabs_key

# === 网易云音乐 (可选) ===
NCM_APP_ID=your_ncm_app_id
NCM_PRIVATE_KEY=your_ncm_private_key
NCM_APP_SECRET=your_ncm_app_secret
```

### 角色数据结构

每个角色在 `users/<user_id>/characters/<char_id>/` 下有独立数据目录：

| 文件 | 说明 |
|------|------|
| `chat.db` | SQLite 聊天记录 |
| `prompts/1_base_persona.md` | 核心人设（不含姓名年龄） |
| `prompts/2_relationship.json` | 角色关系图谱 |
| `prompts/3_user_persona.md` | 用户档案 |
| `prompts/4_memory_long.json` | 长期记忆 (周/月总结) |
| `prompts/5_memory_medium.json` | 中期记忆 (日记式) |
| `prompts/6_memory_short.json` | 短期记忆 (事件日志) |
| `prompts/7_schedule.json` | 日程表 |
| `prompts/8_system_prompt.json` | 自定义 Prompt 覆盖 |

## 🚀 快速开始 (Quick Start)

```bash
# 1. 克隆项目
git clone https://github.com/yyyyanshuo/KunigamiProject.git
cd KunigamiProject

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 .env 文件（填入至少 GEMINI_API_KEY）

# 4. 启动服务
python app.py
```

在浏览器打开 `http://127.0.0.1:5000`，主要页面：
- `/` — 联系人
- `/chat/<char_id>` — 单聊
- `/chat/group/<group_id>` — 群聊
- `/moments` — 朋友圈
- `/memory/<char_id>` — 记忆管理
- `/square` — 角色广场
- `/map` — 地图
- `/sakura` — Sakura 樱语 AI Direct Chat
- `/admin/dashboard` — 管理后台 (仅 admin)

## 📝 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。