import os
import asyncio
import logging
import httpx
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException

# ───────── CONFIG ─────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
RENDER_URL     = os.environ["RENDER_URL"]

GEMINI_KEYS = [v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_") and v.strip()]
if not GEMINI_KEYS:
    raise RuntimeError("No GEMINI_KEY_X provided")

MODEL_NAME   = "gemini-3.1-pro-preview"
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

MAX_INPUT_CHARS = 4000
MAX_QUEUE = 100
WORKERS = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ───────── SYSTEM (STATIC → CACHEABLE) ─────────
SYSTEM_TEXT = "انتي بشرية.انتي اسمك سحابه.انتي تتكلمي باللهجة الجزائرية , ولكم مش ضروري. انتي عمرك 19 سنة. اظهري الحس البشري بدون مبالغة.استعملي الايموجيز بذكاء وبدون مبالغة."
SYSTEM_INSTRUCTION = {"parts": [{"text": SYSTEM_TEXT}]}

# ───────── GLOBALS ─────────
queue = asyncio.Queue(MAX_QUEUE)
seen_updates = set()
context_cache = {}  # simple in-memory cache

# ───────── KEY MANAGER ─────────
class Key:
    def __init__(self, value):
        self.value = value
        self.fail = 0
        self.cooldown = 0

class KeyManager:
    def __init__(self, keys):
        self.keys = [Key(k) for k in keys]
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            now = asyncio.get_event_loop().time()
            valid = [k for k in self.keys if k.cooldown <= now]
            if not valid:
                await asyncio.sleep(1)
                return await self.get()
            valid.sort(key=lambda k: k.fail)
            return valid[0]

    async def fail(self, key, code):
        key.fail += 1
        if code == 429 or code >= 500:
            key.cooldown = asyncio.get_event_loop().time() + min(30, 2 ** key.fail)

    async def success(self, key):
        key.fail = 0

# ───────── GEMINI (TOKENS + CACHE + STRUCTURED) ─────────
class Gemini:
    def __init__(self, client, km):
        self.client = client
        self.km = km

    def thinking(self, length):
        if length < 20:
            return {"thinkingConfig": {"thinkingLevel": "minimal"}}
        elif length < 100:
            return {"thinkingConfig": {"thinkingLevel": "low"}}
        return {"thinkingConfig": {"thinkingLevel": "medium"}}

    async def count_tokens(self, contents, key):
        payload = {
            "contents": contents,
            "systemInstruction": SYSTEM_INSTRUCTION
        }
        r = await self.client.post(
            f"{GEMINI_BASE}/{MODEL_NAME}:countTokens",
            headers={"x-goog-api-key": key},
            json=payload,
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("totalTokens", 0)
        return 0

    async def generate(self, text):
        # ─── CACHE HIT ───
        if text in context_cache:
            return context_cache[text]

        contents = [{"role": "user", "parts": [{"text": text}]}]

        for _ in range(len(GEMINI_KEYS)):
            key = await self.km.get()

            # ─── TOKEN AWARENESS ───
            tokens = await self.count_tokens(contents, key.value)

            max_output = max(512, min(2048, 8000 - tokens))

            payload = {
                "contents": contents,
                "systemInstruction": SYSTEM_INSTRUCTION,
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": max_output,
                    "responseMimeType": "application/json",
                    "responseSchema": {
                        "type": "object",
                        "properties": {
                            "reply": {"type": "string"}
                        },
                        "required": ["reply"]
                    },
                    **self.thinking(len(text))
                }
            }

            for attempt in range(3):
                try:
                    r = await self.client.post(
                        f"{GEMINI_BASE}/{MODEL_NAME}:generateContent",
                        headers={"x-goog-api-key": key.value},
                        json=payload,
                        timeout=15
                    )

                    if r.status_code == 200:
                        data = r.json()

                        raw = ""
                        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        for p in parts:
                            if "text" in p:
                                raw = p["text"]
                                break

                        try:
                            parsed = json.loads(raw)
                            reply = parsed.get("reply", "")
                        except:
                            reply = raw

                        if not reply:
                            reply = "ماقدرتش نجاوب درك"

                        # ─── CACHE STORE ───
                        if len(text) > 20:
                            context_cache[text] = reply

                        await self.km.success(key)
                        return reply

                    if r.status_code == 429 or r.status_code >= 500:
                        await self.km.fail(key, r.status_code)
                        await asyncio.sleep(2 ** attempt)
                        continue

                    if r.status_code in (401, 403):
                        await self.km.fail(key, r.status_code)
                        break

                except Exception:
                    await asyncio.sleep(2 ** attempt)

        raise HTTPException(503, "Gemini unavailable")

# ───────── TELEGRAM ─────────
class Telegram:
    def __init__(self, client):
        self.client = client

    async def send(self, chat_id, text):
        for _ in range(2):
            r = await self.client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text[:4000]},
                timeout=10
            )
            if r.status_code == 200:
                return
            await asyncio.sleep(1)

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

# ───────── WORKER ─────────
async def worker():
    while True:
        chat_id, text = await queue.get()
        try:
            await telegram.typing(chat_id)
            reply = await gemini.generate(text)
            await telegram.send(chat_id, reply)
        except Exception:
            await telegram.send(chat_id, "خطأ مؤقت")
        queue.task_done()

# ───────── APP ─────────
app = FastAPI()

http = None
gemini = None
telegram = None
km = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http, gemini, telegram, km
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    http = httpx.AsyncClient(limits=limits)

    km = KeyManager(GEMINI_KEYS)
    gemini = Gemini(http, km)
    telegram = Telegram(http)

    await telegram.webhook()

    for _ in range(WORKERS):
        asyncio.create_task(worker())

    yield
    await http.aclose()

app.router.lifespan_context = lifespan

# ───────── WEBHOOK ─────────
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update_id = data.get("update_id")

    if update_id in seen_updates:
        return {"ok": True}
    seen_updates.add(update_id)

    msg = data.get("message")
    if not msg:
        return {"ok": True}

    text = (msg.get("text") or "")[:MAX_INPUT_CHARS]
    chat_id = msg["chat"]["id"]

    if not text:
        return {"ok": True}

    if queue.full():
        return {"ok": True}

    await queue.put((chat_id, text))
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
