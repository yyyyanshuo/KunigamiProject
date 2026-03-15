# KunigamiProject 🌸

一个轻量级、重沉浸的多角色 AI 聊天应用。支持单聊/群聊、朋友圈、分级记忆系统（短期/中期/长期）、主动消息与表情包系统，致力于提供无尽历史记录与拟人化社交体验。

## ✨ v2.5.0 新特性

- **📌 群聊置顶**: 支持将重要的群聊置顶显示在联系人列表顶部。
- **👥 多选联系人**: 联系人页面支持多选操作，方便批量管理或发起群聊。
- **🖼️ 表情包系统**: 
  - 支持发送和解析专属表情包，支持多套表情库切换与管理。
  - AI 可通过统一的描述词（如 `[表情]开心`）发送表情。
  - **写时随机机制**: 保证历史聊天记录中的表情刷新后不再“变脸”，同时支持同描述下的随机抽取。
- **🔄 群聊记忆优化**: 群聊发言前，AI 能够同步了解群内成员的单聊近况以及他们参与的**其他群聊**的动态，但不重复总结当前群聊，有效避免记忆回声。

## ✨ 功能概览 (Features)

- **💬 多角色聊天系统**
  - 单聊与群聊无缝切换，基于独立数据库保存所有聊天历史。
  - 类似主流 IM 的体验：自动加载历史消息、多气泡连续发送、拟人化打字延迟、智能时间轴分隔。
  - **👋 拍一拍 (Tickle)**: 双击头像发送拍一拍，支持群聊与单聊，自带多语言提示与自定义后缀。
- **🧠 自动化分级记忆系统**
  - **短期记忆 (Short)**: 按天记录的事件级日志，对话与朋友圈互动后自动提取。
  - **中期记忆 (Medium)**: 后台定时任务自动日结，生成日记式总结。
  - **长期记忆 (Long)**: 自动周结/月结，结合 RAG（检索增强生成）机制在对话时精准提取，避免 Prompt 臃肿。
  - **记忆面板**: 提供前端 UI 供用户查看、手动编辑和请求 AI 润色各级记忆。
  - **跨场景同步**: 单聊前自动同步相关群聊记忆，群聊前同步各成员单聊记忆，保持经历连贯。
- **📱 朋友圈 (Moments)**
  - 角色可根据日程与当前事件，在合适的时机主动发布朋友圈（包含文字与伪图片标签）。
  - 用户可点赞、评论，角色会根据性格与关系进行智能回复。
  - 所有的社交互动均会自动沉淀到当日的短期记忆中。
- **🔔 主动消息 (Active Messages)**
  - 基于情绪指数与作息时间，角色会在清晨、深夜或长时间未联系时主动向用户发送消息或邮件/推送通知。
- **⚙️ 灵活配置与双语支持**
  - 兼容 OpenAI 格式 API 与 Google Gemini 原生 SDK。
  - AI 内核支持全局切换日语/中文沉浸式模式，包含系统 Prompt 与记忆总结的全面适配。
  - 角色年龄基于配置的生日每年自动 +1，与核心 Prompt 解耦。

## 🛠️ 技术栈 (Tech Stack)

- **Backend**: Python 3.x, Flask, APScheduler
- **Database**: SQLite3, JSON
- **Frontend**: HTML5, CSS3, Vanilla JavaScript (原生 JS，无庞大框架依赖)
- **AI Integration**: Google Generative AI SDK / OpenRouter (兼容 OpenAI)

## ⚙️ 配置指南 (Setup)

在运行项目前，需在根目录下配置 `.env` 文件：

```env
# Google Gemini 配置
GEMINI_API_KEY=your_gemini_api_key

# OpenRouter (兼容 OpenAI) 配置
USE_OPENROUTER=true
OPENROUTER_KEY=your_openrouter_api_key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

### 角色数据结构

每个角色在 `users/<user_id>/characters/<char_id>/`（或全局 `characters/`）下拥有独立数据，主要包含：

- `chat.db`: 聊天记录数据库。
- `prompts/1_base_persona.md`: 核心人设（不含姓名年龄）。
- `prompts/2_relationship.json`: 角色图谱。
- `prompts/3_user_persona.md`: 用户档案。
- `prompts/4_memory_long.json` ~ `6_memory_short.json`: 分级记忆文件。
- `prompts/7_schedule.json`: 日程表。

## 🚀 快速开始 (Quick Start)

1. **克隆项目**
   ```bash
   git clone https://github.com/yyyyanshuo/KunigamiProject.git
   cd KunigamiProject
   ```

2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

3. **配置环境**
   在根目录下创建 `.env` 文件并填入 API Keys。确保 `configs/` 目录下存在必要的配置文件。

4. **启动服务**
   ```bash
   python app.py
   ```

5. **访问应用**
   在浏览器中打开 `http://127.0.0.1:5000` 即可体验。主要页面：
   - 联系人与群组：`/contacts`
   - 聊天界面：`/chat/<char_id>` 或 `/chat/group/<group_id>`
   - 朋友圈：`/moments`
   - 记忆管理：`/memory/<char_id>`

## 📝 更新日志 (Changelog)

详见 [CHANGELOG.md](docs/CHANGELOG.md)。

---
*Made with ❤️ & Cursor.*