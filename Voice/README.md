# ElevenLabs TTS Test

这是一个仅供本地开发使用的独立 ElevenLabs TTS 调试工具，不接入 Kunigami 的用户系统。正式站点的角色 TTS、试听和音色克隆使用各用户加密保存的个人 ElevenLabs API Key；只有用户语音的 Speech-to-Text 使用主应用 `.env` 中的平台 Key。不要把这个调试服务部署为公开接口。

## 快速开始

1. **准备个人 API Key**:
   打开调试页面后临时输入你自己的 ElevenLabs API Key。调试工具不会读取项目 `.env`，也不会保存该 Key。

2. **安装依赖**:
   ```bash
   pip install fastapi uvicorn requests
   ```

3. **运行服务端**:
   ```bash
   python -m uvicorn server:app --reload
   ```

4. **访问界面**:
   打开浏览器访问 [http://localhost:8000](http://localhost:8000)

## 文件结构
- `index.html`: TTS 测试界面。
- `server.py`: FastAPI 服务端，使用请求中临时提供的个人 Key 和已有音色 ID 生成语音。
