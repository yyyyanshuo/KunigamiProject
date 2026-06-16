# Voice Cloning Project

这是一个简单的声音克隆项目，使用 FastAPI 作为后端，ElevenLabs API 进行声音克隆。

## 快速开始

1. **配置 API KEY**:
   在 `server.py` 中填入你的 ElevenLabs API KEY:
   ```python
   ELEVEN_API_KEY = "你的_API_KEY"
   ```

2. **安装依赖**:
   ```bash
   pip install fastapi uvicorn requests python-multipart
   ```

3. **运行服务端**:
   ```bash
   python -m uvicorn server:app --reload
   ```

4. **访问界面**:
   打开浏览器访问 [http://localhost:8000](http://localhost:8000)

## 文件结构
- `index.html`: 前端界面，负责文件上传和状态显示。
- `server.py`: FastAPI 服务端，处理前端请求并转发给 ElevenLabs。
