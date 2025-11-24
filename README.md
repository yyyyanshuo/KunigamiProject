# KunigamiProject 🌸

> 一个基于 Flask 和 SQLite 的轻量级 AI 聊天应用，支持无尽历史记录、时间感知与流式对话体验。

## ✨ 功能特性 (Features)

- **💾 持久化记忆**: 使用 SQLite 数据库完整保存聊天记录，不再丢失对话上下文。
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
├── app.py              # 后端核心逻辑 (Flask)
├── chat_history.db     # 聊天记录数据库 (自动生成，已忽略)
├── scripts/            # 实用工具脚本 (如导出聊天记录)
├── static/             # 静态资源 (图标、CSS等)
├── templates/          # 前端页面 (chat.html)
├── .env                # 配置文件 (需手动创建)
└── requirements.txt    # 项目依赖
```

## 📝 许可证
MIT License