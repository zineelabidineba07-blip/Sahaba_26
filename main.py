import os
import time
import asyncio
import logging
import json
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Request

# ───────── CONFIG ─────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
RENDER_URL     = os.environ["RENDER_URL"]

GEMINI_KEYS = [v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_") and v.strip()]
if not GEMINI_KEYS:
    raise RuntimeError("No GEMINI_KEY_X provided")

MODEL_NAME   = "gemini-3-flash-preview"
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

MAX_INPUT_CHARS = 4000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

SYSTEM_INSTRUCTION = {
    "parts": [{"text": "رد مختصر باللهجة الجزائرية."}]
}

# ───────── KEY MANAGER ─────────
class KeyManager:
    def __init__(self, keys):
        self.keys = keys
        self.index = 0
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            key = self.keys[self.index]
            self.index = (self.index + 1) % len(self.keys)
            return key

# ───────── GEMINI ─────────
class Gemini:
    def __init__(self, client, km):
        self.client = client
        self.km = km

    async def generate(self, text):
        if len(text) > MAX_INPUT_CHARS:
            raise HTTPException(400, "Input too large")

        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "systemInstruction": SYSTEM_INSTRUCTION,
        }

        for key_attempt in range(len(GEMINI_KEYS)):
            key = await self.km.get()

            for attempt in range(3):
                try:
                    r = await self.client.post(
                        f"{GEMINI_BASE}/{MODEL_NAME}:generateContent",
                        headers={"x-goog-api-key": key},
                        json=payload,
                        timeout=20
                    )

                    if r.status_code == 200:
                        data = r.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]

                    if r.status_code >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue

                    if r.status_code in (401, 403):
                        break  # switch key

                    raise HTTPException(500, r.text)

                except Exception:
                    await asyncio.sleep(2 ** attempt)

        raise HTTPException(503, "Gemini unavailable")

# ───────── TELEGRAM ─────────
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

# ───────── APP ─────────
app = FastAPI()

http = None
gemini = None
telegram = None
km = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http, gemini, telegram, km
    http = httpx.AsyncClient()
    km = KeyManager(GEMINI_KEYS)
    gemini = Gemini(http, km)
    telegram = Telegram(http)

    await telegram.webhook()
    yield
    await http.aclose()

app.router.lifespan_context = lifespan

# ───────── WEBHOOK ─────────
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

# ───────── HEALTH ─────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7860)
