<div align="center">

# KunigamiProject

一个面向多角色对话与记忆管理的 Web 应用  
支持单聊 / 群聊、分级记忆（短期 / 中期 / 长期）、朋友圈 (Moments) 以及主动消息系统。

</div>

## 功能概览

- **多角色聊天系统**
  - 基于 `characters/<char_id>/chat.db` 的多角色架构，每个角色拥有独立对话历史与 Prompt 配置。
  - 支持单聊与群聊，前端自动根据 URL 识别聊天对象或群组。

- **分级记忆系统**
  - **短期记忆 (Short)**：按天记录精简事件列表 (`6_memory_short.json`)，用于近期上下文。
  - **中期记忆 (Medium)**：按天的日记式总结 (`5_memory_medium.json`)，由后台日结 / 手动触发生成。
  - **长期记忆 (Long)**：按周 / 月的高层总结 (`4_memory_long.json`)，为角色提供稳定世界观与长期经历。
  - 支持从前端记忆面板查看、编辑和 AI 重写各级记忆。

- **朋友圈 (Moments)**
  - 角色与用户都可以发朋友圈，展示近期状态与图片占位标记（如 `[写真（说明）]`）。
  - 点赞与评论支持滚动定位和轻量级交互动效，头像可跳转到角色记忆页或“我的”页面。
  - 角色在「发朋友圈 / 评论用户朋友圈 / 回复用户评论」后，会通过 AI 总结一句话写入当日短期记忆，保证记忆与社交行为同步。

- **主动消息与拍一拍**
  - 基于情绪指数与作息的主动消息系统，角色可在合适时间主动发起聊天或群聊互动。
  - 拍一拍系统支持单聊 / 群聊的拟人化“拍一拍”反馈，并与人设、后缀配置联动。

- **多语言支持**
  - AI 内核支持日语 / 中文两种模式，记忆生成与对话均根据当前语言切换相应 Prompt。
  - 人设 / 记忆 / 输出格式规则集中在 `configs/` 与 `prompts/` 目录，方便统一管理。

## 目录结构（简要）

- `app.py`：Flask 主应用，包含聊天、记忆、朋友圈、主动消息等主要 API。
- `characters/`：各角色的数据目录（数据库、Prompt 与记忆文件）。
- `configs/`：全局配置（角色列表、用户设置、全局格式与人设等）。
- `templates/`：前端页面模板（聊天、记忆、朋友圈、联系人、个人中心等）。
- `static/`：静态资源（CSS / JS / 图片 / PWA 相关文件）。
- `memory_jobs.py`：后台定时任务（记忆日结 / 周结、作息检查、主动消息与朋友圈任务等）。

## 开发与运行

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 启动开发服务器：

```bash
python app.py
```

3. 浏览器访问：

- 聊天页面：`/chat/<char_id>`
- 记忆面板：`/memory/<char_id>`
- 朋友圈：`/moments`
- 个人中心：`/profile`

> 注意：本项目依赖外部 LLM 服务，请在本地配置相应的 API Key 与路由（详见 `app.py` 中的模型配置部分），并根据需要调整 `configs/global_format.md` 与角色人设文件。

# KunigamiProject 🌸

> 一个基于 Flask 和 SQLite 的轻量级 AI 聊天应用，支持无尽历史记录、时间感知、流式对话、多级记忆系统 (Hierarchical Memory)、自动化记忆整理与全日语沉浸式体验。

## ✨ v2.3.0 新特性

- **👋 拍一拍 (Tickle)**: 双击头像发送拍一拍，支持单聊/群聊，多语言展示（日/中），可配置后缀。
- **🔄 记忆连贯**: 单聊前自动同步群聊记忆，群聊前自动同步各成员单聊记忆，保持跨场景记忆连贯。
- **🧠 多级记忆系统**: 短期(Short)、中期(Medium)、长期(Long) 三级记忆自动流转。
- **⏰ 自动化整理**: 后台定时任务自动进行日结和周结，无需人工干预。
- **🎌 双语支持**: 支持日语/中文切换，系统 Prompt 与记忆总结均可适配。
- **📝 动态 Prompt**: 模块化管理 Prompt 文件，支持热更新。

## ✨ 功能特性 (Features)

- **💾 持久化记忆**: 使用 SQLite 数据库完整保存聊天记录，不再丢失对话上下文。
- **🔄 记忆连贯**: 单聊前自动同步群聊记忆，群聊前自动同步各成员单聊记忆。
- **👋 拍一拍**: 双击头像发送拍一拍，支持多语言展示与自定义后缀。
- **📜 无限滚动**: 类似主流 IM 软件的体验，向上滚动自动加载历史消息。
- **⏰ 时间感知**: AI 能够感知当前的日期和时间，对话更加自然真实。
- **🫧 多气泡渲染**: 支持 AI 连续发送多条消息（多气泡），并带有模拟打字延迟，拟人化程度高。
- **📅 智能时间轴**: 聊天记录按日期自动分组，显示美观的日期分隔符。
- **🔄 双模型支持**: 灵活切换 Google Gemini 官方 API 或 OpenRouter（兼容 OpenAI 格式）接口。
- **📋 便捷操作**: 支持一键复制消息气泡内容，输入框高度自适应。

## 🛠️ 技术栈 (Tech Stack)

- **Backend**: Python 3.x, Flask
- **Database**: SQLite3
- **Frontend**: HTML5, CSS3, Vanilla JavaScript (原生 JS，无庞大框架依赖)
- **AI Integration**: Google Generative AI SDK / OpenAI Compatible API

## ⚙️ 配置指南 (Setup Prompts)

为了保护隐私，本项目仓库中**未包含**具体的角色设定文件。
在运行项目前，请务必在 `prompts/` 目录下手动创建以下文件：

| 文件名 | 类型 | 说明 | 示例内容 |
| :--- | :--- | :--- | :--- |
| **1_base_persona.md** | Markdown | **角色核心设定**。包含性格、口癖、背景故事。**不包含姓名和年龄**（姓名以 `characters.json` 的 `name` 为准，年龄在记忆页面单独编辑，每年自动+1）。 | `プロサッカー選手。身長180cm...` |
| **2_relationship.json** | JSON | **角色关系图谱**。Key为用户名。 | `{"UserName": {"role": "恋人", "description": "..."}}` |
| **3_user_persona.md** | Markdown | **用户档案**。AI 需要知道的关于你的信息。 | `ユーザーは大学生で、性格は...` |
| **4_memory_long.json** | JSON | **长期记忆** (按月/周)。 | `{"2025-10": "出会いの季節..."}` |
| **5_memory_medium.json** | JSON | **中期记忆** (最近7天)。 | `{"2025-12-01": "今日は雨だった..."}` |
| **6_memory_short.json** | JSON | **短期记忆** (当天)。 | `{"2025-12-03": [{"time":"10:00","event":"..."}]}` |
| **7_schedule.json** | JSON | **日程安排**。 | `{"2025-12-25": "クリスマスの予定"}` |

> **提示**: `6_memory_short.json` 和 `5_memory_medium.json` 如果没有内容，可以留一个空的 JSON 对象 `{}`，系统运行后会自动生成。

## 🚀 快速开始 (Quick Start)

### 1. 克隆项目
```bash
git clone https://github.com/YourUsername/KunigamiProject.git
cd KunigamiProject
```

### 2. 环境配置
建议使用 Python 虚拟环境：
```bash
# Windows
python -m venv venv
.\venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

安装依赖：
```bash
pip install -r requirements.txt
```

### 3. 配置环境变量
在项目根目录创建 `.env` 文件，填入你的 API Key：

```ini
# 选择一：使用 Google Gemini (推荐)
GEMINI_API_KEY=your_gemini_api_key_here

# 选择二：使用 OpenRouter (或其他 OpenAI 兼容接口)
USE_OPENROUTER=true
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_KEY=your_openrouter_key_here
```

### 4. 运行应用
```bash
python app.py
```
启动后，访问浏览器：`http://127.0.0.1:5000` 即可开始聊天。



## 📂 项目结构

```text
KunigamiProject
├── app.py              # 后端核心逻辑 (Flask + APScheduler)
├── memory_jobs.py      # 记忆整理任务脚本
├── prompts/            # Prompt 模块文件夹 (核心!)
│   ├── 8_format.md     # [已包含] 输出格式规范
│   └── (其他文件需自行创建，详见下方配置指南)
├── static/             # 静态资源 (头像、CSS)
├── templates/          # 前端页面
├── chat_history.db     # 数据库 (自动生成)
└── requirements.txt    # 依赖列表
```

## 📝 许可证
MIT License