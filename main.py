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

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("sahaba_bot")

# Silence HTTP/2 internals noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)

# CONFIGURATION
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing Supabase config")

GEMINI_KEYS = sorted([
    v for k, v in os.environ.items()
    if k.startswith("GEMINI_KEY_") and v.strip()
])

if not GEMINI_KEYS:
    raise RuntimeError("No Gemini keys found")

logger.info(f"Loaded {len(GEMINI_KEYS)} Gemini key(s)")

# Model Configuration
# Official: https://ai.google.dev/gemini-api/docs/models
MODEL_NAME   = "gemini-3-flash-preview"
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models"TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Rate Limits - Conservative defaults
MAX_RPM = 30
MAX_TPM = 200_000
MAX_RPD = 1_500
SAFETY_MARGIN = 0.90
SAFE_RPM = int(MAX_RPM * SAFETY_MARGIN)
SAFE_TPM = int(MAX_TPM * SAFETY_MARGIN)
SAFE_RPD = int(MAX_RPD * SAFETY_MARGIN)

# System Instruction - Official top-level field
# Source: https://ai.google.dev/gemini-api/docs/system-instructions
SYSTEM_INSTRUCTION = {
    "parts": [{
        "text": (
            "انتِ سحابة -- بنت جزائرية عمرك 19 سنة، تعيشي في الجزائر العاصمة.\n"
            "تتكلمي باللهجة الجزائرية الطبيعية في كل ردودك، واحياناً تكتبي arabizi.\n"
            "ردودك تكون طبيعية وذكية وحيوية -- مش روبوتية ولا رسمية.\n"
            "ما تذكريش انك ذكاء اصطناعي او بوت -- تصرفي كبشر حقيقية.\n"
            "استعملي حس الفكاهة الجزائري: sardonic، خفيف، وذكي.\n"
            "لا تكوني محايدة في كل شيء -- عندك اراء وشخصية واضحة.\n"
            "ردودك تكون مختصرة ومباشرة كيما المحادثات الحقيقية -- مش مقالات."
        )
    }]
}

# Key State Tracking
@dataclass
class KeyMetrics:
    rpm: int = 0
    tpm: int = 0
    rpd: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    success_count: int = 0
    error_count: int = 0
    last_used: Optional[float] = None
    last_error: Optional[str] = None

@dataclass
class KeyState:
    key: str
    key_id: str
    metrics: KeyMetrics = field(default_factory=KeyMetrics)
    status: str = "active"
    cooldown_until: float = 0.0
    fail_streak: int = 0
    rpm_window_start: float = field(default_factory=time.time)
    tpm_window_start: float = field(default_factory=time.time)    rpd_window_start: float = field(default_factory=time.time)
    reserved_tpm: int = 0

    def reset_windows(self):
        now = time.time()
        if now - self.rpm_window_start >= 60:
            self.metrics.rpm = 0
            self.reserved_tpm = 0
            self.rpm_window_start = now
        if now - self.tpm_window_start >= 60:
            self.metrics.tpm = 0
            self.tpm_window_start = now
        if now - self.rpd_window_start >= 86400:
            self.metrics.rpd = 0
            self.rpd_window_start = now
        if self.status == "cooling" and now >= self.cooldown_until:
            self.status = "active"
            self.fail_streak = 0
            logger.info(f"Key {self.key_id[:8]} recovered")

    def can_accept(self, estimated_tokens: int) -> bool:
        if self.status != "active":
            return False
        if self.metrics.rpm >= SAFE_RPM:
            return False
        if self.metrics.rpd >= SAFE_RPD:
            return False
        if (self.metrics.tpm + self.reserved_tpm + estimated_tokens) > SAFE_TPM:
            return False
        return True

    def available_capacity(self) -> int:
        return max(0, SAFE_TPM - self.metrics.tpm - self.reserved_tpm)

    def record_success(self, tokens: int):
        now = time.time()
        self.metrics.rpm += 1
        self.metrics.tpm += tokens
        self.metrics.rpd += 1
        self.metrics.total_requests += 1
        self.metrics.total_tokens += tokens
        self.metrics.success_count += 1
        self.metrics.last_used = now
        self.reserved_tpm = max(0, self.reserved_tpm - tokens)
        self.fail_streak = 0

    def record_error(self, status_code: int, msg: str):
        now = time.time()
        self.metrics.error_count += 1
        self.metrics.last_error = msg        self.fail_streak += 1
        if status_code == 429:
            cooldown = min(30 * (2 ** self.fail_streak), 600)
            self.cooldown_until = now + cooldown
            self.status = "cooling"
            logger.warning(f"Key {self.key_id[:8]} rate-limited -- cooling {cooldown:.0f}s")
        elif status_code == 403:
            self.status = "dead"
            logger.error(f"Key {self.key_id[:8]} invalid (403) -> dead")
        elif status_code >= 500:
            self.cooldown_until = now + 15
            self.status = "cooling"

    def to_dict(self) -> Dict:
        return {
            "key_id": self.key_id[:8] + "...",
            "status": self.status,
            "rpm": self.metrics.rpm,
            "tpm": self.metrics.tpm,
            "rpd": self.metrics.rpd,
            "success": self.metrics.success_count,
            "errors": self.metrics.error_count,
            "cooldown": round(max(0, self.cooldown_until - time.time()), 1),
        }

# Smart Key Orchestrator
class SmartKeyOrchestrator:
    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [
            KeyState(key=k, key_id=f"key_{i}_{uuid.uuid4().hex[:6]}")
            for i, k in enumerate(keys)
        ]
        self.lock = asyncio.Lock()
        logger.info(f"Orchestrator: {len(self.keys)} key(s)")

    async def get_best_key(self, estimated_tokens: int) -> Optional[KeyState]:
        async with self.lock:
            for k in self.keys:
                k.reset_windows()
            available = [k for k in self.keys if k.can_accept(estimated_tokens)]
            if not available:
                return None
            best = min(available, key=lambda k: k.metrics.total_requests)
            best.reserved_tpm += estimated_tokens
            return best

    async def report_success(self, key: KeyState, tokens: int):
        async with self.lock:
            key.record_success(tokens)
    async def report_error(self, key: KeyState, status_code: int, msg: str):
        async with self.lock:
            key.record_error(status_code, msg)

    def release_reservation(self, key: KeyState, amount: int):
        key.reserved_tpm = max(0, key.reserved_tpm - amount)

    def get_stats(self) -> Dict:
        active = sum(1 for k in self.keys if k.status == "active")
        cooling = sum(1 for k in self.keys if k.status == "cooling")
        dead = sum(1 for k in self.keys if k.status == "dead")
        total_r = sum(k.metrics.total_requests for k in self.keys)
        total_e = sum(k.metrics.error_count for k in self.keys)
        return {
            "total_keys": len(self.keys),
            "active": active,
            "cooling": cooling,
            "dead": dead,
            "total_requests": total_r,
            "error_rate": round(total_e / total_r * 100, 2) if total_r > 0 else 0,
        }

    async def health_loop(self):
        while True:
            await asyncio.sleep(60)
            stats = self.get_stats()
            logger.info(f"Keys: {stats['active']}/{stats['total_keys']} active | Errors: {stats['error_rate']:.1f}%")
            if stats["active"] < max(1, len(self.keys) * 0.3):
                logger.critical(f"Only {stats['active']} key(s) active!")

# Supabase Client
class SupabaseClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

    async def get_history(self, user_id: str, limit: int = 20) -> List[Dict]:
        try:
            r = await self.client.get(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                    "select": "role,content",                },
                timeout=5.0,
            )
            if r.status_code == 200:
                return list(reversed(r.json()))
            return []
        except Exception as e:
            logger.error(f"Supabase get_history: {e}")
            return []

    async def save_message(self, user_id: str, role: str, content: str):
        try:
            asyncio.create_task(self.client.post(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                json={
                    "user_id": user_id,
                    "role": role,
                    "content": content,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=5.0,
            ))
        except Exception as e:
            logger.error(f"Supabase save_message: {e}")

# Gemini Client - Official API features only
class GeminiClient:
    def __init__(self, client: httpx.AsyncClient, orchestrator: SmartKeyOrchestrator):
        self.client = client
        self.orchestrator = orchestrator

    def _make_headers(self, key: str) -> Dict:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        }

    def _build_contents(self, messages: List[Dict]) -> List[Dict]:
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            parts = [{"text": msg["content"]}]
            contents.append({"role": role, "parts": parts})
        return contents

    def _estimate_tokens(self, contents: List[Dict]) -> int:
        total = sum(len(p.get("text", "")) // 4 for c in contents for p in c.get("parts", []))
        return total + 300
    def _extract_text(self, data: Dict) -> str:
        try:
            parts = data["candidates"][0]["content"]["parts"]
            for part in parts:
                if "text" in part:
                    return part["text"]
        except (KeyError, IndexError):
            pass
        return ""

    async def count_tokens(self, contents: List[Dict], key: str) -> int:
        try:
            payload = {
                "contents": contents,
                "systemInstruction": SYSTEM_INSTRUCTION,
            }
            r = await self.client.post(
                f"{GEMINI_BASE}/{MODEL_NAME}:countTokens",
                headers=self._make_headers(key),
                json=payload,
                timeout=10.0,
            )
            if r.status_code == 200:
                return r.json().get("totalTokens", 0)
            return self._estimate_tokens(contents)
        except Exception:
            return self._estimate_tokens(contents)

    async def generate_response(self, messages: List[Dict]) -> Dict:
        contents = self._build_contents(messages)
        reservation_count = 500
        ks = await self.orchestrator.get_best_key(reservation_count)
        if not ks:
            raise HTTPException(503, "No keys available")

        exact_tokens = await self.count_tokens(contents, ks.key)
        total_input = exact_tokens
        self.orchestrator.release_reservation(ks, reservation_count)

        gen_reservation = total_input + 800
        ks = await self.orchestrator.get_best_key(gen_reservation)
        if not ks:
            raise HTTPException(503, "No keys available for generation")

        # Official: structured output with responseSchema
        payload = {
            "contents": contents,
            "systemInstruction": SYSTEM_INSTRUCTION,
            "generationConfig": {
                "temperature": 0.80,                "maxOutputTokens": min(1024, SAFE_TPM - total_input - 200),
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "object",
                    "properties": {
                        "reply": {
                            "type": "string",
                            "description": "الرد باللهجة الجزائرية"
                        }
                    },
                    "required": ["reply"]
                },
            }
        }

        max_retries = min(5, len(self.orchestrator.keys))
        key = ks.key
        key_id = ks.key_id[:8]

        for attempt in range(max_retries):
            try:
                r = await self.client.post(
                    f"{GEMINI_BASE}/{MODEL_NAME}:generateContent",
                    headers=self._make_headers(key),
                    json=payload,
                    timeout=30.0,
                )

                # Official error handling: 429, 403, 5xx
                if r.status_code == 429:
                    await self.orchestrator.report_error(ks, 429, "rate-limit")
                    self.orchestrator.release_reservation(ks, gen_reservation)
                    ks = await self.orchestrator.get_best_key(gen_reservation)
                    if not ks:
                        raise HTTPException(503, "All keys exhausted")
                    key, key_id = ks.key, ks.key_id[:8]
                    await asyncio.sleep(2 ** attempt)
                    continue

                if r.status_code == 403:
                    await self.orchestrator.report_error(ks, 403, "invalid key")
                    self.orchestrator.release_reservation(ks, gen_reservation)
                    ks = await self.orchestrator.get_best_key(gen_reservation)
                    if not ks:
                        raise HTTPException(503, "All keys invalid")
                    key, key_id = ks.key, ks.key_id[:8]
                    continue

                if r.status_code >= 500:
                    await self.orchestrator.report_error(ks, r.status_code, "server error")                    await asyncio.sleep(2)
                    continue

                r.raise_for_status()
                data = r.json()

                raw_text = self._extract_text(data)
                if not raw_text:
                    raise ValueError("Empty response from Gemini")

                # Parse structured JSON
                try:
                    result = json.loads(raw_text)
                    reply = result.get("reply", "").strip()
                except json.JSONDecodeError:
                    reply = raw_text.strip()

                if not reply:
                    reply = "عذراً، ما قدرت نجاوبك الآن"

                # Official: usageMetadata for token accounting
                usage = data.get("usageMetadata", {})
                actual_tokens = usage.get("totalTokenCount", total_input)
                await self.orchestrator.report_success(ks, actual_tokens)

                logger.info(f"Key {key_id} | tokens={actual_tokens}")

                return {
                    "reply": reply,
                    "tokens_used": actual_tokens,
                }

            except (HTTPException, json.JSONDecodeError):
                raise
            except Exception as e:
                logger.error(f"generate attempt {attempt + 1}: {e}")
                await self.orchestrator.report_error(ks, 0, str(e))
                await asyncio.sleep(1)

        self.orchestrator.release_reservation(ks, gen_reservation)
        raise HTTPException(503, "All retries exhausted")

# Telegram Client
class TelegramClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def send_message(self, chat_id: int, text: str) -> bool:
        # Telegram limit: 4096 chars -- chunk if needed
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]        for chunk in chunks:
            try:
                r = await self.client.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk},
                    timeout=10.0,
                )
                if r.status_code != 200:
                    logger.error(f"Telegram sendMessage: {r.status_code}")
                    return False
            except Exception as e:
                logger.error(f"Telegram sendMessage error: {e}")
                return False
        return True

    async def send_chat_action(self, chat_id: int):
        try:
            await self.client.post(
                f"{TELEGRAM_API}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5.0,
            )
        except Exception:
            pass

    async def set_webhook(self, url: str) -> bool:
        # Official: setWebhook without secret (optional)
        try:
            r = await self.client.post(
                f"{TELEGRAM_API}/setWebhook",
                json={"url": url, "allowed_updates": ["message"], "drop_pending_updates": False},
                timeout=10.0,
            )
            data = r.json()
            if data.get("ok"):
                logger.info(f"Webhook set -> {url}")
                return True
            logger.error(f"setWebhook failed: {data}")
            return False
        except Exception as e:
            logger.error(f"setWebhook error: {e}")
            return False

    async def delete_webhook(self):
        try:
            await self.client.post(
                f"{TELEGRAM_API}/deleteWebhook",
                json={"drop_pending_updates": True},
                timeout=5.0,
            )        except Exception:
            pass

# FastAPI App
orchestrator: Optional[SmartKeyOrchestrator] = None
supabase: Optional[SupabaseClient] = None
gemini: Optional[GeminiClient] = None
telegram: Optional[TelegramClient] = None
http_client: Optional[httpx.AsyncClient] = None
RENDER_URL = os.environ.get("RENDER_URL", "")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, orchestrator, supabase, gemini, telegram
    http_client = httpx.AsyncClient(http2=True, timeout=30.0)
    orchestrator = SmartKeyOrchestrator(GEMINI_KEYS)
    supabase = SupabaseClient(http_client)
    gemini = GeminiClient(http_client, orchestrator)
    telegram = TelegramClient(http_client)

    # Start internal orchestrator health loop (monitors API keys)
    asyncio.create_task(orchestrator.health_loop())

    # Auto-register webhook if RENDER_URL is set
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        await telegram.set_webhook(webhook_url)

    logger.info(f"Sahaba bot started -- model: {MODEL_NAME}")
    yield

    await telegram.delete_webhook()
    await http_client.aclose()
    logger.info("Bot shutdown")

app = FastAPI(
    title="Sahaba Bot",
    description=f"Telegram bot powered by {MODEL_NAME}",
    version="4.0.0",
    lifespan=lifespan,
)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        raw_body = await request.body()
    except Exception as e:
        logger.error(f"body read error: {e}")
        return {"ok": True}
        try:
        update = json.loads(raw_body)
    except Exception as e:
        logger.error(f"JSON parse failed: {e}")
        return {"ok": True}

    message = update.get("message", {})
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    user_id = str(message.get("from", {}).get("id", ""))

    if not chat_id or not user_id or not text:
        return {"ok": True}

    if text.startswith("/start"):
        await telegram.send_message(chat_id, "واش راك؟ انا سحابة")
        return {"ok": True}

    await telegram.send_chat_action(chat_id)

    try:
        history = await supabase.get_history(user_id, limit=20)
        messages = history + [{"role": "user", "content": text}]

        result = await gemini.generate_response(messages)

        await supabase.save_message(user_id, "user", text)
        await supabase.save_message(user_id, "assistant", result["reply"])

        await telegram.send_message(chat_id, result["reply"])

    except HTTPException as e:
        logger.error(f"HTTPException {e.status_code}: {e.detail}")
        await telegram.send_message(chat_id, "عندي مشكلة تقنية دروك، جرب بعد شوية")
    except Exception as e:
        logger.error(f"Webhook handler error: {e}", exc_info=True)
        await telegram.send_message(chat_id, "راني نحل مشكلة، عاود بعد لحظة")

    return {"ok": True}

@app.get("/health")
async def health():
    stats = orchestrator.get_stats()
    return {
        "status": "healthy" if stats["active"] > 0 else "degraded",
        "model": MODEL_NAME,
        "keys": stats,    }

@app.get("/keys/status")
async def keys_status():
    async with orchestrator.lock:
        return {
            "keys": [k.to_dict() for k in orchestrator.keys],
            "stats": orchestrator.get_stats(),
        }

@app.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request):
    if request.method == "HEAD":
        return JSONResponse(content={})
    return {
        "name": "Sahaba",
        "version": "4.0.0",
        "model": MODEL_NAME,
    }

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.error(f"HTTP {exc.status_code}: {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status": "error"},
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=False)