import os
import time
import asyncio
import json
import re
import httpx
from fastapi import FastAPI, HTTPException, Request
from typing import List, Dict, Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

# ───────── CONFIG ─────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL   = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
RENDER_URL     = os.environ["RENDER_URL"]

GEMINI_KEYS = sorted([v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_")])

MODEL = "gemini-3-flash-preview"
BASE  = "https://generativelanguage.googleapis.com/v1beta/models"
TG    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ───────── SYSTEM PROMPT ─────────
SYSTEM = {
    "parts": [{"text": "تكلمي بالدارجة الجزائرية، خفيفة وطبيعية."}]
}

# ───────── KEY STATE ─────────
@dataclass
class Key:
    key: str
    rpm: int = 0
    last: float = 0

class KeyManager:
    def __init__(self, keys):
        self.keys = [Key(k) for k in keys]
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            k = min(self.keys, key=lambda x: x.rpm)
            k.rpm += 1
            k.last = time.time()
            return k

key_manager = KeyManager(GEMINI_KEYS)

# ───────── RATE CONTROL ─────────
class Rate:
    def __init__(self):
        self.last = 0

    async def wait(self):
        now = time.time()
        if now - self.last < 0.05:
            await asyncio.sleep(0.05)
        self.last = time.time()

rate = Rate()

# ───────── SUPABASE ─────────
class DB:
    def __init__(self, client):
        self.client = client
        self.headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

    async def history(self, uid):
        r = await self.client.get(
            f"{SUPABASE_URL}/rest/v1/messages",
            headers=self.headers,
            params={"user_id": f"eq.{uid}", "order": "created_at.desc", "limit": "20"}
        )
        return list(reversed(r.json())) if r.status_code == 200 else []

    async def save(self, data):
        for i in range(3):
            r = await self.client.post(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                json=data
            )
            if r.status_code in (200,201,204):
                return
            await asyncio.sleep(2**i)
        raise RuntimeError("DB write failed")

# ───────── GEMINI ─────────
class Gemini:
    def __init__(self, client):
        self.client = client

    def build(self, msgs):
        out = []
        for m in msgs:
            out.append({
                "role": "user" if m["role"]=="user" else "model",
                "parts": [{"text": m["content"]}]
            })
        return out

    def parse(self, text):
        try:
            return json.loads(text)
        except:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        raise ValueError("bad json")

    async def run(self, msgs):
        contents = self.build(msgs)

        k = await key_manager.get()

        await rate.wait()

        payload = {
            "contents": contents,
            "systemInstruction": SYSTEM,
            "generationConfig": {
                "temperature": 0.8,
                "maxOutputTokens": 512,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type":"object",
                    "properties":{"reply":{"type":"string"}},
                    "required":["reply"]
                },
                "thinkingConfig":{"thinkingLevel":"low"}
            }
        }

        r = await self.client.post(
            f"{BASE}/{MODEL}:generateContent",
            headers={"x-goog-api-key": k.key},
            json=payload
        )

        if r.status_code != 200:
            raise HTTPException(503, "gemini error")

        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]

        try:
            return self.parse(text)["reply"]
        except:
            return text

# ───────── TELEGRAM ─────────
class TGClient:
    def __init__(self, client):
        self.client = client

    async def send(self, cid, txt):
        await self.client.post(f"{TG}/sendMessage", json={"chat_id":cid,"text":txt})

    async def webhook(self):
        await self.client.post(
            f"{TG}/setWebhook",
            json={
                "url": f"{RENDER_URL}/webhook/{WEBHOOK_SECRET}",
                "secret_token": WEBHOOK_SECRET
            }
        )

# ───────── APP ─────────
client = httpx.AsyncClient(timeout=20)
db = DB(client)
ai = Gemini(client)
tg = TGClient(client)

@asynccontextmanager
async def life(app):
    await tg.webhook()
    yield

app = FastAPI(lifespan=life)

@app.post("/webhook/{secret}")
async def hook(secret: str, req: Request):

    if secret != WEBHOOK_SECRET:
        raise HTTPException(403)

    if req.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403)

    data = await req.json()
    msg = data.get("message", {})

    text = msg.get("text")
    cid  = msg.get("chat", {}).get("id")
    uid  = str(msg.get("from", {}).get("id"))

    if not text:
        return {"ok":True}

    hist = await db.history(uid)
    msgs = hist + [{"role":"user","content":text}]

    reply = await ai.run(msgs)

    await db.save({"user_id":uid,"role":"user","content":text})
    await db.save({"user_id":uid,"role":"assistant","content":reply})

    await tg.send(cid, reply)

    return {"ok":True}

@app.get("/health")
def health():
    return {"status":"ok"}
