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