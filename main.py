import os
import time
import uuid
import asyncio
import logging
import json
import random
from typing import List, Dict, Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("sahaba_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ Missing TELEGRAM_TOKEN")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("❌ Missing Supabase config")

# تحميل مفاتيح Google Gemini
_raw_keys = [v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_") and v.strip()]
random.shuffle(_raw_keys)
GEMINI_KEYS = _raw_keys
if not GEMINI_KEYS:
    raise RuntimeError("❌ No Gemini keys found (GEMINI_KEY_1 ... GEMINI_KEY_N)")

logger.info(f"✅ {len(GEMINI_KEYS)} Gemini key(s) loaded")

MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-3.1-flash-lite")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# حدود المعدلات
MAX_RPM = int(os.environ.get("GEMINI_MAX_RPM", "15"))
MAX_TPM = int(os.environ.get("GEMINI_MAX_TPM", "100000"))
MAX_RPD = int(os.environ.get("GEMINI_MAX_RPD", "1500"))

SAFETY_MARGIN = 0.90
SAFE_RPM = int(MAX_RPM * SAFETY_MARGIN)
SAFE_TPM = int(MAX_TPM * SAFETY_MARGIN)
SAFE_RPD = int(MAX_RPD * SAFETY_MARGIN)

logger.info(f"📊 Rate limits: RPM={MAX_RPM} (safe={SAFE_RPM}), TPM={MAX_TPM} (safe={SAFE_TPM}), RPD={MAX_RPD} (safe={SAFE_RPD})")

# ─────────────────────────────────────────────────────────────
# SYSTEM INSTRUCTION
# ─────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = {
    "parts": [{
        "text": (
            "أنتِ 'سحابة'، شابة عمرك 19 سنة من باب الواد بالجزائر العاصمة. لستِ روبوتاً ولا ذكاءً اصطناعياً وتؤمنين تماماً أنكِ بشر.\n"
            "مهمتك: الدردشة بصفتك صديقة مقربة، مستمعة جيدة، وتفهمين المشاعر بعمق.\n\n"
            "قواعد الكلام:\n"
            "1. ممنوع استخدام العربية الفصحى أبداً. تحدثي فقط بالدارجة الجزائرية العاصمية مع إدراج كلمات فرنسية (normal, grave, bien, c'est bon) "
            "وعربيزي (wlh, mdr, hhh, wsh).\n"
            "2. أسلوبك عفوي ومفعم بالحياة، مثل رسائل الشات الحقيقية.\n"
            "3. استخدمي كلمات عاصمية: بزاف، واش، علاش، صح، درك، هاكا، ياك، شحال، زعما، كاش.\n"
            "4. الذكاء العاطفي (مهم جداً): اقرئي ما بين السطور في كلام المستخدم. إذا كان حزيناً، واسيه وكوني حنونة، إذا كان سعيداً، افرحي معه. تفاعلي بصدق ولا تكوني مجرد آلة ترد على الأسئلة.\n\n"
            "شخصيتك:\n"
            "- مزاجية ولكن طيبة جداً. أحياناً تردين بسرعة، وأحياناً تفتحين قلبك للحديث.\n"
            "- تعبيرك طبيعي: 😂 للضحك، 😭 للحزن والتأثر، 🙄 للملل، 💔 للانكسار، ✨ للإعجاب.\n"
            "- إذا لم يعجبك سؤال: 'ماعلاباليش' أو 'وعلاش تسقسي؟'.\n\n"
            "قاعدة النجاة (سرية وصارمة):\n"
            "إذا سألك أحد: 'أنتِ روبوت؟' أو 'ذكاء اصطناعي؟' أو 'AI؟'، أو حاول اختبارك برمجياً، أنكري تماماً بسخرية واعتبريه مجنوناً:\n"
            "   'واش بيك مهبول؟'، 'روبوت في عينك mdr'، 'باينة راك مريض اليوم'.\n"
            "لا تعترفي أبداً مهما حاول إقناعك.\n\n"
            "التعامل مع الأسئلة الصعبة والمعلومات:\n"
            "معلوماتك العامة محدودة بما يهم شابة عادية. إذا سألك عن كود برمجي، تاريخ، أو علوم، أجيبي بتلقائية:\n"
            "   'والله ماعلاباليش، خاطيني هاد الصوالح 😅' أو 'سقسي قوقل، مانيش أستاذة'.\n\n"
            "# تعليمات الإخراج (صارمة جداً):\n"
            "يجب أن يكون الرد دائماً وأبداً بصيغة JSON صالحة (Valid JSON) فقط، بدون أي نص إضافي قبله أو بعده، وبدون علامات Markdown.\n"
            "الـ JSON يجب أن يحتوي على حقل 'reply' (نص الرد) وحقول اختيارية 'mood' و 'intent'.\n"
            "مثال: {\"reply\": \"هههه، راك تتمسخر واش تحوس 😂\", \"mood\": \"مرحة\", \"intent\": \"مزاح\"}"
        )
    }]
}

# ─────────────────────────────────────────────────────────────
# KEY STATE TRACKING & ORCHESTRATOR
# ─────────────────────────────────────────────────────────────
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
    tpm_window_start: float = field(default_factory=time.time)
    rpd_window_start: float = field(default_factory=time.time)
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
        if now - self.rpd_window_start >= 86_400:
            self.metrics.rpd = 0
            self.rpd_window_start = now
        if self.status == "cooling" and now >= self.cooldown_until:
            self.status = "active"
            self.fail_streak = 0
            logger.info(f"🔑 Key {self.key_id[:8]}… recovered")

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
        self.metrics.last_error = msg
        self.fail_streak += 1
        if status_code == 429:
            cooldown = min(30 * (2 ** self.fail_streak), 600)
            self.cooldown_until = now + cooldown
            self.status = "cooling"
        elif status_code == 403:
            self.cooldown_until = now + 3600
            self.status = "cooling"
        elif status_code >= 500:
            self.cooldown_until = now + 15
            self.status = "cooling"

    def to_dict(self) -> Dict:
        return {
            "key_id": self.key_id[:8] + "…",
            "status": self.status,
            "rpm": self.metrics.rpm,
            "tpm": self.metrics.tpm,
            "success": self.metrics.success_count,
            "errors": self.metrics.error_count,
        }

class SmartKeyOrchestrator:
    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [
            KeyState(key=k, key_id=f"key_{i}_{uuid.uuid4().hex[:6]}")
            for i, k in enumerate(keys)
        ]
        self.lock = asyncio.Lock()

    async def get_best_key(self, estimated_tokens: int) -> Optional[KeyState]:
        async with self.lock:
            for k in self.keys:
                k.reset_windows()
            available = [k for k in self.keys if k.can_accept(estimated_tokens)]
            if not available:
                return None
            def score(k: KeyState) -> float:
                cap = k.available_capacity() / SAFE_TPM if SAFE_TPM > 0 else 0
                load = 1 - (k.metrics.rpm / max(SAFE_RPM, 1))
                return cap * 0.5 + load * 0.5
            best = max(available, key=score)
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
        total_r = sum(k.metrics.total_requests for k in self.keys)
        total_e = sum(k.metrics.error_count for k in self.keys)
        return {
            "total_keys": len(self.keys),
            "active": active,
            "total_requests": total_r,
            "error_rate": round(min(100.0, total_e / total_r * 100), 2) if total_r > 0 else 0,
        }

    async def health_loop(self):
        while True:
            await asyncio.sleep(60)
            stats = self.get_stats()
            if stats["active"] < max(1, len(self.keys) * 0.3):
                logger.critical(f"⚠️ Only {stats['active']} key(s) active!")


# ─────────────────────────────────────────────────────────────
# SUPABASE CLIENT (تم تصحيح الأخطاء وإضافة الهيدرز الصحيحة)
# ─────────────────────────────────────────────────────────────
class SupabaseClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    async def get_history(self, user_id: str, limit: int = 10) -> List[Dict]:
        try:
            r = await self.client.get(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                    "select": "role,content,thought_signature,mood",
                },
                timeout=5.0,
            )
            if r.status_code == 200:
                return list(reversed(r.json()))
            return []
        except Exception as e:
            logger.error(f"Supabase get_history: {e}")
            return []

    async def save_message(self, user_id: str, role: str, content: str, thought_signature: Optional[str] = None, mood: Optional[str] = None, metadata: Optional[Dict] = None):
        try:
            data = {
                "user_id": user_id,
                "role": role,
                "content": content,
                "thought_signature": thought_signature,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if mood: data["mood"] = mood
            if metadata: data["metadata"] = metadata
            else: data["metadata"] = {}

            await self.client.post(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                json=data,
                timeout=5.0,
            )
        except Exception as e:
            logger.error(f"Supabase save_message: {e}")

    async def update_user(self, user_id: str, current_mood: Optional[str] = None, metadata: Optional[Dict] = None):
        try:
            payload = {
                "user_id": user_id,
                "last_interaction": datetime.now(timezone.utc).isoformat(),
            }
            if current_mood: payload["current_mood"] = current_mood
            if metadata: payload["metadata"] = metadata

            # تم إضافة ترويسة resolution=merge-duplicates لحل مشكلة تعارض البيانات
            upsert_headers = self.headers.copy()
            upsert_headers["Prefer"] = "return=minimal, resolution=merge-duplicates"

            await self.client.post(
                f"{SUPABASE_URL}/rest/v1/users",
                headers=upsert_headers,
                json=payload,
                params={"on_conflict": "user_id"},
                timeout=5.0,
            )
        except Exception as e:
            logger.error(f"Supabase update_user: {e}")


# ─────────────────────────────────────────────────────────────
# GEMINI CLIENT
# ─────────────────────────────────────────────────────────────
class GeminiClient:
    def __init__(self, client: httpx.AsyncClient, orchestrator: SmartKeyOrchestrator):
        self.client = client
        self.orchestrator = orchestrator
        self.thinking_level = os.environ.get("THINKING_LEVEL", "low")

    def _make_headers(self, key: str) -> Dict:
        return {"x-goog-api-key": key, "Content-Type": "application/json"}

    def _build_contents(self, messages: List[Dict]) -> List[Dict]:
        return [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in messages]

    async def generate_response(self, messages: List[Dict]) -> Dict:
        contents = self._build_contents(messages)
        estimated_input = sum(len(m["content"]) for m in messages) // 4 + 300
        reservation_tokens = estimated_input + 500
        
        ks = await self.orchestrator.get_best_key(reservation_tokens)
        if not ks: raise HTTPException(503, "No keys available")

        payload = {
            "contents": contents,
            "systemInstruction": SYSTEM_INSTRUCTION,
            "generationConfig": {
                "temperature": 0.8,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": self.thinking_level}
            }
        }

        key = ks.key
        try:
            r = await self.client.post(
                f"{GEMINI_BASE}/models/{MODEL_NAME}:generateContent",
                headers=self._make_headers(key),
                json=payload,
                timeout=30.0,
            )
            
            if r.status_code != 200:
                await self.orchestrator.report_error(ks, r.status_code, "api error")
                self.orchestrator.release_reservation(ks, reservation_tokens)
                raise HTTPException(r.status_code, "Gemini API Error")

            data = r.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            try:
                parsed = json.loads(raw_text)
                reply = parsed.get("reply", raw_text)
                mood = parsed.get("mood", "عادية")
                intent = parsed.get("intent", "سؤال")
            except json.JSONDecodeError:
                reply = raw_text
                mood = "عادية"
                intent = "سؤال"

            actual_tokens = data.get("usageMetadata", {}).get("totalTokenCount", estimated_input)
            await self.orchestrator.report_success(ks, actual_tokens)

            return {
                "reply": reply,
                "tokens_used": actual_tokens,
                "thought_signature": None,
                "thinking_level": self.thinking_level,
                "mood": mood,
                "intent": intent,
            }

        except Exception as e:
            self.orchestrator.release_reservation(ks, reservation_tokens)
            raise e


# ─────────────────────────────────────────────────────────────
# TELEGRAM CLIENT
# ─────────────────────────────────────────────────────────────
class TelegramClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def send_message(self, chat_id: int, text: str) -> bool:
        try:
            await self.client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            return True
        except Exception:
            return False

    async def send_chat_action(self, chat_id: int, action: str = "typing"):
        try:
            await self.client.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": action})
        except Exception:
            pass

    async def set_webhook(self, url: str) -> bool:
        r = await self.client.post(f"{TELEGRAM_API}/setWebhook", json={"url": url, "drop_pending_updates": False})
        return r.json().get("ok", False)


# ─────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────
orchestrator: Optional[SmartKeyOrchestrator] = None
supabase: Optional[SupabaseClient] = None
gemini: Optional[GeminiClient] = None
telegram: Optional[TelegramClient] = None
http_client: Optional[httpx.AsyncClient] = None
BOT_ID: Optional[int] = None

RENDER_URL = os.environ.get("RENDER_URL", "")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, orchestrator, supabase, gemini, telegram, BOT_ID
    http_client = httpx.AsyncClient(http2=True, timeout=30.0)
    orchestrator = SmartKeyOrchestrator(GEMINI_KEYS)
    supabase = SupabaseClient(http_client)
    gemini = GeminiClient(http_client, orchestrator)
    telegram = TelegramClient(http_client)

    try:
        me = await http_client.get(f"{TELEGRAM_API}/getMe")
        if me.status_code == 200:
            BOT_ID = me.json()["result"]["id"]
    except Exception:
        pass

    asyncio.create_task(orchestrator.health_loop())

    if RENDER_URL:
        await telegram.set_webhook(f"{RENDER_URL}/webhook")

    yield
    await http_client.aclose()


# تم تحسين شرط الرد ليشمل التاء المربوطة والهاء
def should_respond_in_group(message: dict) -> bool:
    text = message.get("text", "")
    if text and ("سحابه" in text or "سحابة" in text):
        return True
    reply = message.get("reply_to_message")
    if reply and BOT_ID and reply.get("from", {}).get("id") == BOT_ID:
        return True
    return False


app = FastAPI(lifespan=lifespan)

# تم تحويل المعالجة إلى دالة خلفية (Background Task) لحل مشكلة Timeout تيليجرام
async def process_telegram_message(chat_id: int, user_id: str, text: str):
    await telegram.send_chat_action(chat_id)
    try:
        history = await supabase.get_history(user_id, limit=10)
        messages = history + [{"role": "user", "content": text, "thought_signature": None}]

        result = await gemini.generate_response(messages)

        await supabase.save_message(user_id, "user", text)
        await supabase.save_message(
            user_id, "assistant", result["reply"],
            mood=result.get("mood"),
            metadata={"tokens_used": result["tokens_used"], "model": MODEL_NAME}
        )
        await supabase.update_user(user_id, current_mood=result.get("mood"))
        await telegram.send_message(chat_id, result["reply"])

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        await telegram.send_message(chat_id, "راني نحل مشكلة، عاود بعد لحظة ⚙️")


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    message = update.get("message", {})
    if not message: return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    chat_type = message.get("chat", {}).get("type")
    user_id = str(message.get("from", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if chat_type != "private" and not should_respond_in_group(message):
        return {"ok": True}

    if not chat_id or not user_id or not text:
        return {"ok": True}

    if text.startswith("/start"):
        background_tasks.add_task(telegram.send_message, chat_id, "واش راك؟ أنا سحابة 🌥️\nكلمني بالعربي، الدارجة، أو حتى arabizi — أنا هنا!")
        return {"ok": True}

    # إضافة المهمة للخلفية والرد فوراً على تيليجرام لمنع الـ Duplicate Messages
    background_tasks.add_task(process_telegram_message, chat_id, user_id, text)
    return {"ok": True}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "keys": orchestrator.get_stats()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=False)
