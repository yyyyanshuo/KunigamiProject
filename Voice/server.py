from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import io
import requests

app = FastAPI()

# 允许跨域以便调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.post("/tts")
async def tts(data: dict):
    api_key = str(data.get("api_key") or "").strip()
    text = str(data.get("text") or "").strip()
    voice_id = str(data.get("voice_id") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写你自己的 ElevenLabs API Key")
    if not text or not voice_id:
        raise HTTPException(status_code=400, detail="请填写 Voice ID 和文本")

    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json"
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2"
        }
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="ElevenLabs TTS 请求失败")

    return StreamingResponse(
        io.BytesIO(response.content),
        media_type="audio/mpeg"
    )
