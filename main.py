import os
import asyncio
import logging
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request

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

# ───────── KEY MANAGER (ROUND-ROBIN) ─────────
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

# ───────── GEMINI (RETRY + FAILOVER) ─────────
class Gemini:
    def __init__(self, client, km):
        self.client = client
        self.km = km

    async def generate(self, text):
        payload = {
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "systemInstruction": SYSTEM_INSTRUCTION,
        }

        for _ in range(len(GEMINI_KEYS)):
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
                        break

                except Exception:
                    await asyncio.sleep(2 ** attempt)

        return "عندي ضغط شوية، عاود بعد لحظة 🙏"

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

    async def typing(self, chat_id):
        try:
            await self.client.post(
                f"{TELEGRAM_API}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5
            )
        except:
            pass

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

# ───────── NON-BLOCKING PROCESSOR ─────────
async def process_message(chat_id, text):
    await telegram.typing(chat_id)

    try:
        reply = await gemini.generate(text)
    except Exception:
        reply = "خطأ مؤقت"

    await telegram.send(chat_id, reply)

# ───────── WEBHOOK (FAST RETURN) ─────────
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

    asyncio.create_task(process_message(chat_id, text))

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
