from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import requests
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / '.env')

app = FastAPI()

# 允许跨域以便调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.post("/clone_voice")
async def clone_voice(file: UploadFile = File(...)):

    audio_data = await file.read()

    response = requests.post(
        "https://api.elevenlabs.io/v1/voices/add",
        headers={
            "xi-api-key": ELEVEN_API_KEY
        },
        files={
            "files": (
                file.filename,
                audio_data,
                file.content_type
            )
        },
        data={
            "name": "rensuke_voice"
        }
    )

    return response.json()

from fastapi.responses import StreamingResponse
import io

@app.post("/tts")
async def tts(data: dict):

    text = data["text"]
    voice_id = data["voice_id"]

    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": ELEVEN_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2"
        }
    )

    return StreamingResponse(
        io.BytesIO(response.content),
        media_type="audio/mpeg"
    )