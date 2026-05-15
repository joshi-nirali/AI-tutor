"""
Legacy FastAPI + local .imx demo (manual LiveKit tracks).

For the kid tutor product flow (bitHuman cloud + LiveKit Agents + React UI), use:
  1. python token_server.py
  2. python agent.py dev
  3. cd frontend && npm start

See .env.example for LIVEKIT_* and BITHUMAN_AGENT_ID.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
import os
import threading

import cv2
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

import requests
from pydantic import BaseModel
import uvicorn

from bithuman import AsyncBithuman
from bithuman.audio import float32_to_int16, load_audio

from livekit.rtc import Room, VideoTrack, AudioTrack

load_dotenv()

# =========================
# ENV VARIABLES
# =========================
BITHUMAN_API_SECRET = os.getenv("BITHUMAN_API_SECRET")
LIVEKIT_TOKEN_URL = os.getenv("LIVEKIT_TOKEN_URL", "http://127.0.0.1:5000/token")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

print("BITHUMAN_API_SECRET:", "FOUND" if BITHUMAN_API_SECRET else "MISSING")
print("LIVEKIT_TOKEN_URL:", LIVEKIT_TOKEN_URL)

# =========================
# AUDIO BUFFER
# =========================
audio_buf = bytearray()
audio_lock = threading.Lock()
avatar_ready = False
runtime_instance = None

def audio_callback(outdata, frames, _time, _status):
    n_bytes = frames * 2
    with audio_lock:
        available = min(len(audio_buf), n_bytes)
        outdata[:available // 2, 0] = np.frombuffer(audio_buf[:available], dtype=np.int16)
        outdata[available // 2:, 0] = 0
        del audio_buf[:available]

# =========================
# FASTAPI SETUP
# =========================
app = FastAPI()

app.mount("/audio", StaticFiles(directory="."), name="audio")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Message(BaseModel):
    text: str
    mode: str

@app.on_event("startup")
async def startup_event():
    print("🚀 FastAPI started")
    asyncio.create_task(avatar_loop())

# =========================
# PROMPT BUILDER
# =========================
def build_prompt(mode):
    return f"""
You are a friendly AI tutor for kids.

Mode: {mode}

If Vocabulary Mode:
- Introduce word
- Explain meaning simply
- Give example sentence
- Ask student to repeat
- Ask one question

If Speaking Mode:
- Ask student to repeat word
- Correct pronunciation gently

If Quiz Mode:
- Ask question
- Wait for answer
- Give feedback

Rules:
- Always say "Good try!"
- Never say "wrong"
- Use simple English
- Be interactive
"""

# =========================
# AI RESPONSE
# =========================
async def get_ai_response(user_text, mode):
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": build_prompt(mode)},
                {"role": "user", "content": user_text}
            ]
        )
        return response.choices[0].message.content

    except Exception as e:
        print("❌ OpenAI Error:", e)
        return "Good try! Let's learn together. Can you tell me more?"

# =========================
# TTS + AVATAR AUDIO
# =========================
async def speak_text(runtime, text):
    import edge_tts
    output_file = "response.wav"

    communicate = edge_tts.Communicate(text, voice="en-US-AriaNeural")
    await communicate.save(output_file)

    audio_np, sr = load_audio(output_file)
    audio_np = float32_to_int16(audio_np)

    await runtime.push_audio(audio_np.tobytes(), sr, last_chunk=True)
    with audio_lock:
        audio_buf.extend(audio_np.tobytes())

# =========================
# CHAT ENDPOINT
# =========================
@app.post("/chat")
async def chat(msg: Message):
    ai_reply = await get_ai_response(msg.text, msg.mode)

    audio_file = None

    if avatar_ready and runtime_instance:
        asyncio.create_task(speak_text(runtime_instance, ai_reply))
    else:
        audio_file = await simple_tts(ai_reply)

    return {
        "reply": ai_reply,
        "avatar": avatar_ready,
        "audio_url": f"http://localhost:8000/audio/{audio_file}" if audio_file else None
    }

async def simple_tts(text):
    import edge_tts
    import uuid

    filename = f"audio_{uuid.uuid4().hex}.wav"

    communicate = edge_tts.Communicate(text, voice="en-US-AriaNeural")
    await communicate.save(filename)

    return filename

# =========================
# AVATAR + LIVEKIT LOOP
# =========================
async def avatar_loop():
    global runtime_instance, avatar_ready

    # Check Bithuman secret
    if not BITHUMAN_API_SECRET:
        print("❌ Missing Bithuman API secret")
        return

    # Create BitHuman runtime
    try:
        model_path = os.getenv(
            "BITHUMAN_MODEL_PATH",
            "playful_micro_workout_guide_20251122_112054_802985.imx",
        )
        runtime = await AsyncBithuman.create(
            model_path=model_path,
            api_secret=BITHUMAN_API_SECRET,
        )
        runtime_instance = runtime
        avatar_ready = True
        print("✅ Avatar READY")
    except Exception as e:
        print("❌ Avatar failed:", e)
        return

    # Local audio output
    speaker = sd.OutputStream(
        samplerate=16000,
        channels=1,
        dtype="int16",
        blocksize=640,
        callback=audio_callback
    )
    speaker.start()

    # ===== Connect to LiveKit room =====
    try:
        # fetch token from your token server
        print("📡 Fetching LiveKit token...")
        resp = requests.get(LIVEKIT_TOKEN_URL, timeout=5)
        token = resp.json().get("token")

        if not token:
            raise Exception("No token received")

        print("🔑 LiveKit token:", token[:30], "...")
        room = Room()
        await room.connect(LIVEKIT_URL, token)
        print("🎥 Connected to LiveKit")

        # create tracks
        video_track = VideoTrack()
        audio_track = AudioTrack()
        room.add_track(video_track)
        room.add_track(audio_track)
        print("📡 LiveKit tracks created")

    except Exception as e:
        print("❌ LiveKit connect error:", e)
        video_track = None
        audio_track = None
        room = None

    # ===== Start BitHuman and stream =====
    await runtime.start()

    async for frame in runtime.run():
        if frame.has_image and video_track:
            img = frame.get_image()
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            await video_track.send_frame(rgb)

        if frame.audio_chunk:
            with audio_lock:
                audio_buf.extend(frame.audio_chunk.array.tobytes())
            if audio_track:
                await audio_track.send_audio(frame.audio_chunk.array, samplerate=16000)

    # cleanup
    speaker.stop()
    cv2.destroyAllWindows()
    await runtime.stop()

# =========================
# RUN
# =========================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)