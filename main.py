import os
import time
import uuid
import asyncio
import logging
import json
from typing import List, Dict, Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ───────────────────────── CONFIG ─────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL   = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
RENDER_URL     = os.environ["RENDER_URL"]

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not RENDER_URL:
    raise RuntimeError("Missing required environment variables")

GEMINI_KEYS = [v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_") and v.strip()]
if not GEMINI_KEYS:
    raise RuntimeError("No GEMINI_KEY_X provided")

MODEL_NAME = "gemini-3-flash-preview"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

MAX_INPUT_CHARS = 4000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ───────────────────────── SYSTEM ─────────────────────────
SYSTEM_INSTRUCTION = {
    "parts": [{"text": "رد مختصر باللهجة الجزائرية."}]
}

# ───────────────────────── KEY ─────────────────────────
@dataclass
class KeyState:
    key: str
    last_used: float = 0
    errors: int = 0

class KeyManager:
    def __init__(self, keys):
        self.keys = [KeyState(k) for k in keys]
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            self.keys.sort(key=lambda k: (k.errors, k.last_used))
            k = self.keys[0]
            k.last_used = time.time()
            return k

    async def fail(self, key):
        key.errors += 1

# ───────────────────────── CLIENTS ─────────────────────────
class Gemini:
    def __init__(self, client, km):
        self.client = client
        self.km = km

    async def generate(self, text):
        if len(text) > MAX_INPUT_CHARS:
            raise HTTPException(400, "Input too large")

        key = await self.km.get()

        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "systemInstruction": SYSTEM_INSTRUCTION,
        }

        r = await self.client.post(
            f"{GEMINI_BASE}/{MODEL_NAME}:generateContent",
            headers={"x-goog-api-key": key.key},
            json=payload,
            timeout=20
        )

        if r.status_code != 200:
            await self.km.fail(key)
            raise HTTPException(500, r.text)

        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

class Telegram:
    def __init__(self, client):
        self.client = client

    async def send(self, chat_id, text):
        await self.client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )

    async def webhook(self):
        await self.client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": f"{RENDER_URL}/webhook"},
        )

# ───────────────────────── APP ─────────────────────────
km = None
gemini = None
telegram = None
http = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global km, gemini, telegram, http
    http = httpx.AsyncClient()
    km = KeyManager(GEMINI_KEYS)
    gemini = Gemini(http, km)
    telegram = Telegram(http)

    await telegram.webhook()
    yield
    await http.aclose()

app = FastAPI(lifespan=lifespan)

# ───────────────────────── WEBHOOK ─────────────────────────
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    msg = data.get("message")
    if not msg:
        return {"ok": True}

    text = (msg.get("text") or "")[:MAX_INPUT_CHARS]
    chat_id = msg["chat"]["id"]

    if not text:
        return {"ok": True}

    try:
        reply = await gemini.generate(text)
    except Exception:
        reply = "خطأ مؤقت"

    await telegram.send(chat_id, reply)
    return {"ok": True}

# ───────────────────────── HEALTH ─────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7860)
