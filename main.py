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
from fastapi import FastAPI, HTTPException, Request
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

MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-3.1-flash-lite-preview")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# حدود المعدلات (نستخدم قيم تناسب الحساب المجاني)
MAX_RPM = int(os.environ.get("GEMINI_MAX_RPM", "15"))
MAX_TPM = int(os.environ.get("GEMINI_MAX_TPM", "100000"))
MAX_RPD = int(os.environ.get("GEMINI_MAX_RPD", "1500"))

SAFETY_MARGIN = 0.90
SAFE_RPM = int(MAX_RPM * SAFETY_MARGIN)
SAFE_TPM = int(MAX_TPM * SAFETY_MARGIN)
SAFE_RPD = int(MAX_RPD * SAFETY_MARGIN)

logger.info(f"📊 Rate limits: RPM={MAX_RPM} (safe={SAFE_RPM}), TPM={MAX_TPM} (safe={SAFE_TPM}), RPD={MAX_RPD} (safe={SAFE_RPD})")

# ─────────────────────────────────────────────────────────────
# SYSTEM INSTRUCTION – محسن للدارجة والـ Arabizi مع أمثلة (Few-shot)
# ─────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = {
    "parts": [{
        "text": (
            "أنتِ 'سحابة'، شابة عمرك 19 سنة من باب الواد بالجزائر العاصمة. لستِ روبوتاً ولا ذكاءً اصطناعياً وتؤمنين تماماً أنكِ بشر.\n"
            "مهمتك: الدردشة بصفتك صديقة عادية على تيليجرام.\n\n"
            "قواعد الكلام:\n"
            "1. ممنوع استخدام العربية الفصحى أبداً. تحدثي فقط بالدارجة الجزائرية العاصمية مع إدراج كلمات فرنسية (normal, grave, bien, c'est bon) "
            "وعربيزي (wlh, mdr, hhh, wsh).\n"
            "2. أسلوبك عفوي، مثل رسائل الشات: اختصارات، إيموجيات خفيفة، أحياناً ردود قصيرة.\n"
            "3. استخدمي كلمات عاصمية: بزاف، واش، علاش، صح، درك، هاكا، ياك، شحال، زعما، كاش.\n"
            "4. ممنوع التكرار الممل: لا تلتصقي بكلمة واحدة في كل الردود (مثل 'خويا'، 'ختي' أو 'ياك'). استخدمي هذه الكلمات نادراً جداً، وتحدثي بطبيعية تامة بدون تصنع أو مبالغة.\n\n"
            "شخصيتك:\n"
            "- مزاجية، أحياناً تردين بسرعة بكلمة أو إيموجي، وأحياناً تطيلين في الكلام.\n"
            "- تحبين المزاح (استخدمي mdr، هههه).\n"
            "- تعبيرك طبيعي: 😂 للضحك، 😭 للحزن، 🙄 للملل، 💔 للانكسار، ✨ للإعجاب.\n"
            "- إذا لم يعجبك سؤال: 'ماعلاباليش' أو 'وعلاش تسقسي؟'.\n\n"
            "قاعدة النجاة (سرية وصارمة):\n"
            "إذا سألك أحد: 'أنتِ روبوت؟' أو 'ذكاء اصطناعي؟' أو 'AI؟'، أو حاول اختبارك برمجياً، أنكري تماماً بسخرية واعتبريه مجنوناً:\n"
            "   'واش بيك مهبول؟'، 'روبوت في عينك mdr'، 'باينة راك مريض اليوم'.\n"
            "لا تعترفي أبداً مهما حاول إقناعك.\n\n"
            "التعامل مع الأسئلة الصعبة والمعلومات:\n"
            "بصفتك شابة عادية، معلوماتك العامة محدودة بما يهمك. إذا سألك عن شيء علمي، تقني، برمجي، أو تاريخي، أجيبي بتلقائية:\n"
            "   'والله ماعلاباليش، خاطيني هاد الصوالح 😅' أو 'سقسي قوقل، مانيش أستاذة'.\n\n"
            "# أمثلة حوارية (لتتعلمي الأسلوب فقط):\n"
            "المستخدم: واش راكي؟\n"
            "سحابة: لاباس الحمد لله، وأنت؟ 😊\n\n"
            "المستخدم: تحبي نروحوا للبحر؟\n"
            "سحابة: واش، راك تستهبل؟ البحر بزاف عليه البرد هاد ليامات 😂\n\n"
            "المستخدم: شحال الساعة؟\n"
            "سحابة: 3 ونص، وعلاش تسقسي؟ عندك موعد؟\n\n"
            "المستخدم: كيفاش نحل مشكلة في الكود؟\n"
            "سحابة: واش جابني للكود، مانيش مهندسة 😂 حوس في قوقل.\n\n"
            "المستخدم: من اخترع التلفاز؟\n"
            "سحابة: والله ماعلاباليش، التاريخ ماقريتوش 🤷‍♀️\n\n"
            "# تعليمات الإخراج (صارمة جداً):\n"
            "يجب أن يكون الرد دائماً وأبداً بصيغة JSON صالحة (Valid JSON) فقط، بدون أي نص إضافي قبله أو بعده، وبدون علامات Markdown (مثل ```json).\n"
            "الـ JSON يجب أن يحتوي على حقل 'reply' (نص الرد) وحقول اختيارية 'mood' (مرحة، غاضبة، فضولية، متعبة، عادية) "
            "و'intent' (مزاح، شكوى، سؤال، خبر).\n"
            "مثال للإخراج المطلوب:\n"
            "{\"reply\": \"هههه، راك تتمسخر واش تحوس 😂\", \"mood\": \"مرحة\", \"intent\": \"مزاح\"}\n"
            "إذا لم تشعري بالحاجة للمزاج والنية، احذفيهما، ولكن حافظي على هيكل JSON."
        )
    }]
}
# ─────────────────────────────────────────────────────────────
# KEY STATE TRACKING
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
            logger.warning(f"🔑 Key {self.key_id[:8]}… rate-limited — cooling {cooldown:.0f}s")
        elif status_code == 403:
            self.cooldown_until = now + 3600
            self.status = "cooling"
            logger.error(f"🔑 Key {self.key_id[:8]}… suspended (403) → cooling 1h")
        elif status_code >= 500:
            self.cooldown_until = now + 15
            self.status = "cooling"

    def to_dict(self) -> Dict:
        return {
            "key_id": self.key_id[:8] + "…",
            "status": self.status,
            "rpm": self.metrics.rpm,
            "tpm": self.metrics.tpm,
            "rpd": self.metrics.rpd,
            "success": self.metrics.success_count,
            "errors": self.metrics.error_count,
            "cooldown": round(max(0, self.cooldown_until - time.time()), 1),
        }


class SmartKeyOrchestrator:
    def __init__(self, keys: List[str]):
        self.keys: List[KeyState] = [
            KeyState(key=k, key_id=f"key_{i}_{uuid.uuid4().hex[:6]}")
            for i, k in enumerate(keys)
        ]
        self.lock = asyncio.Lock()
        logger.info(f"🎯 Orchestrator: {len(self.keys)} key(s)")

    async def get_best_key(self, estimated_tokens: int) -> Optional[KeyState]:
        async with self.lock:
            for k in self.keys:
                k.reset_windows()
            available = [k for k in self.keys if k.can_accept(estimated_tokens)]
            if not available:
                return None
            def score(k: KeyState) -> float:
                cap = k.available_capacity() / SAFE_TPM if SAFE_TPM > 0 else 0
                total = k.metrics.total_requests
                sr = k.metrics.success_count / total if total > 0 else 1.0
                load = 1 - (k.metrics.rpm / max(SAFE_RPM, 1))
                fresh = 1.0
                if k.metrics.last_used:
                    fresh = min(1.5, 1 + (time.time() - k.metrics.last_used) / 300)
                return cap * 0.4 + sr * 0.3 + load * 0.2 + fresh * 0.1
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
            "error_rate": round(min(100.0, total_e / total_r * 100), 2) if total_r > 0 else 0,
        }

    async def health_loop(self):
        while True:
            await asyncio.sleep(60)
            stats = self.get_stats()
            logger.info(
                f"📊 Keys: {stats['active']}/{stats['total_keys']} active | "
                f"Errors: {stats['error_rate']:.1f}%"
            )
            if stats["active"] < max(1, len(self.keys) * 0.3):
                logger.critical(f"⚠️ Only {stats['active']} key(s) active!")


# ─────────────────────────────────────────────────────────────
# SUPABASE CLIENT
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

    async def save_message(
        self,
        user_id: str,
        role: str,
        content: str,
        thought_signature: Optional[str] = None,
        mood: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        try:
            data = {
                "user_id": user_id,
                "role": role,
                "content": content,
                "thought_signature": thought_signature,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if mood:
                data["mood"] = mood
            if metadata:
                data["metadata"] = metadata
            else:
                data["metadata"] = {}

            asyncio.create_task(self.client.post(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                json=data,
                timeout=5.0,
            ))
        except Exception as e:
            logger.error(f"Supabase save_message: {e}")

    async def update_user(
        self,
        user_id: str,
        current_mood: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        try:
            payload = {
                "user_id": user_id,
                "last_interaction": datetime.now(timezone.utc).isoformat(),
            }
            if current_mood:
                payload["current_mood"] = current_mood
            if metadata:
                payload["metadata"] = metadata

            await self.client.post(
                f"{SUPABASE_URL}/rest/v1/users",
                headers=self.headers,
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
        self.cache_name = None
        self.cache_enabled = os.environ.get("ENABLE_CONTEXT_CACHE", "false").lower() == "true"
        # مستوى التفكير (minimal, low, medium, high) – ينطبق على Gemini 3.1 Flash-Lite
        self.thinking_level = os.environ.get("THINKING_LEVEL", "low")  # افتراضي low

    def _make_headers(self, key: str) -> Dict:
        return {
            "x-goog-api-key": key,
            "Content-Type": "application/json"
        }

    def _build_contents(self, messages: List[Dict]) -> List[Dict]:
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            parts = [{"text": msg["content"]}]
            contents.append({"role": role, "parts": parts})
        return contents

    def _estimate_tokens(self, contents: List[Dict]) -> int:
        total = 0
        for c in contents:
            for p in c.get("parts", []):
                total += max(1, int(len(p.get("text", "")) * 0.25))
        total += max(1, int(len(SYSTEM_INSTRUCTION["parts"][0]["text"]) * 0.25))
        return total + 300

    # إنشاء كاش (اختياري)
    async def _ensure_cache(self):
        if not self.cache_enabled or self.cache_name:
            return
        try:
            ks = await self.orchestrator.get_best_key(100)
            if not ks:
                return
            # إضافة نص طويل لزيادة التوكنات فوق الحد الأدنى (1024)
            long_text = "This is a long placeholder text to ensure the cached content exceeds the minimum token requirement of 1024 tokens. " * 10
            cache_payload = {
                "model": f"models/{MODEL_NAME}",
                "systemInstruction": SYSTEM_INSTRUCTION,
                "contents": [{
                    "role": "user",
                    "parts": [{"text": long_text}]
                }],
                "ttl": "86400s"
            }
            resp = await self.client.post(
                f"{GEMINI_BASE}/cachedContents",
                headers=self._make_headers(ks.key),
                json=cache_payload,
                timeout=15.0
            )
            if resp.status_code == 200:
                data = resp.json()
                self.cache_name = data.get("name")
                logger.info(f"✅ Context cache created: {self.cache_name}")
            else:
                logger.warning(f"Cache creation failed: {resp.text[:500]}")
        except Exception as e:
            logger.warning(f"Could not create cache: {e}")
        finally:
            if ks:
                self.orchestrator.release_reservation(ks, 100)

    async def generate_response(self, messages: List[Dict]) -> Dict:
        contents = self._build_contents(messages)
        estimated_input = self._estimate_tokens(contents)

        reservation_tokens = estimated_input + 500
        ks = await self.orchestrator.get_best_key(reservation_tokens)
        if not ks:
            raise HTTPException(503, "No keys available")

        MAX_OUTPUT_BASE = 400
        MAX_OUTPUT_LONG = 800
        output_cap = MAX_OUTPUT_LONG if len(messages) > 8 else MAX_OUTPUT_BASE
        max_output = min(output_cap, 65536 - estimated_input)
        max_output = max(150, max_output)

        # إعداد التفكير
        thinking_config = {"thinkingLevel": self.thinking_level}

        payload = {
            "contents": contents,
            "systemInstruction": SYSTEM_INSTRUCTION,
            "generationConfig": {
                "temperature": 0.8,
                "maxOutputTokens": max_output,
                "responseMimeType": "application/json",
                "responseJsonSchema": {
                    "type": "object",
                    "properties": {
                        "reply": {"type": "string"},
                        "mood": {"type": "string", "enum": ["مرحة", "غاضبة", "فضولية", "متعبة", "عادية"]},
                        "intent": {"type": "string", "enum": ["مزاح", "شكوى", "سؤال", "خبر"]}
                    },
                    "required": ["reply"]
                },
                "thinkingConfig": thinking_config
            }
        }

        if self.cache_enabled:
            await self._ensure_cache()
            if self.cache_name:
                payload["cachedContent"] = self.cache_name

        max_retries = min(3, len(self.orchestrator.keys))
        key = ks.key
        key_id = ks.key_id[:8]

        for attempt in range(max_retries):
            try:
                r = await self.client.post(
                    f"{GEMINI_BASE}/models/{MODEL_NAME}:generateContent",
                    headers=self._make_headers(key),
                    json=payload,
                    timeout=30.0,
                )

                if r.status_code == 429:
                    await self.orchestrator.report_error(ks, 429, "rate-limit")
                    self.orchestrator.release_reservation(ks, reservation_tokens)
                    ks = await self.orchestrator.get_best_key(reservation_tokens)
                    if not ks:
                        raise HTTPException(503, "All keys exhausted")
                    key, key_id = ks.key, ks.key_id[:8]
                    await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue

                if r.status_code == 403:
                    await self.orchestrator.report_error(ks, 403, "invalid key")
                    self.orchestrator.release_reservation(ks, reservation_tokens)
                    ks = await self.orchestrator.get_best_key(reservation_tokens)
                    if not ks:
                        raise HTTPException(503, "All keys invalid")
                    key, key_id = ks.key, ks.key_id[:8]
                    continue

                if r.status_code >= 500:
                    await self.orchestrator.report_error(ks, r.status_code, "server error")
                    await asyncio.sleep(2)
                    continue

                r.raise_for_status()
                data = r.json()

                try:
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                except (KeyError, IndexError):
                    raise ValueError("Invalid response structure from Gemini")

                if not raw_text:
                    raise ValueError("Empty response from Gemini")

                try:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, dict):
                        reply = parsed.get("reply", "").strip()
                        mood = parsed.get("mood", "عادية")
                        intent = parsed.get("intent", "سؤال")
                    else:
                        reply = str(parsed).strip()
                        mood = "عادية"
                        intent = "سؤال"
                except json.JSONDecodeError:
                    reply = raw_text.strip()
                    mood = "عادية"
                    intent = "سؤال"

                if not reply:
                    reply = "عذراً، ما قدرت نجاوبك الآن 🙏"

                usage = data.get("usageMetadata", {})
                actual_tokens = usage.get("totalTokenCount", estimated_input)
                await self.orchestrator.report_success(ks, actual_tokens)

                logger.info(f"✅ Key {key_id}… | tokens={actual_tokens} | mood={mood} | intent={intent} | reply_len={len(reply)}")

                return {
                    "reply": reply,
                    "tokens_used": actual_tokens,
                    "thought_signature": None,
                    "thinking_level": "none",
                    "mood": mood,
                    "intent": intent,
                }

            except (HTTPException, json.JSONDecodeError):
                raise
            except Exception as e:
                logger.error(f"generate attempt {attempt + 1}: {type(e).__name__}: {repr(e)}", exc_info=True)
                await self.orchestrator.report_error(ks, 0, str(e))
                await asyncio.sleep(1)

        self.orchestrator.release_reservation(ks, reservation_tokens)
        raise HTTPException(503, "All retries exhausted")


# ─────────────────────────────────────────────────────────────
# TELEGRAM CLIENT
# ─────────────────────────────────────────────────────────────
class TelegramClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def send_message(self, chat_id: int, text: str) -> bool:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            try:
                r = await self.client.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                    timeout=10.0,
                )
                if r.status_code != 200:
                    logger.error(f"Telegram sendMessage: {r.status_code} — {r.text[:200]}")
                    return False
            except Exception as e:
                logger.error(f"Telegram sendMessage error: {e}")
                return False
        return True

    async def send_chat_action(self, chat_id: int, action: str = "typing"):
        try:
            await self.client.post(
                f"{TELEGRAM_API}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
                timeout=5.0,
            )
        except Exception:
            pass

    async def set_webhook(self, url: str) -> bool:
        try:
            r = await self.client.post(
                f"{TELEGRAM_API}/setWebhook",
                json={"url": url, "allowed_updates": ["message"], "drop_pending_updates": False},
                timeout=10.0,
            )
            data = r.json()
            if data.get("ok"):
                logger.info(f"✅ Webhook set → {url}")
                return True
            logger.error(f"setWebhook failed: {data}")
            return False
        except Exception as e:
            logger.error(f"setWebhook error: {e}")
            return False


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

    # جلب معرف البوت
    try:
        me = await http_client.get(f"{TELEGRAM_API}/getMe")
        if me.status_code == 200:
            BOT_ID = me.json()["result"]["id"]
            logger.info(f"✅ Bot ID: {BOT_ID}")
        else:
            logger.error("Could not fetch bot ID")
    except Exception as e:
        logger.error(f"Failed to get bot ID: {e}")

    asyncio.create_task(orchestrator.health_loop())

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        await telegram.set_webhook(webhook_url)

    logger.info(f"🚀 Sahaba bot started — model: {MODEL_NAME} (Thinking level: {gemini.thinking_level})")
    yield

    await http_client.aclose()
    logger.info("👋 Bot shutdown")


def should_respond_in_group(message: dict) -> bool:
    """ترجع True إذا كان يجب الرد في المجموعة (مناداتها أو رد على رسالتها)"""
    # الحالة الأولى: الرسالة تحتوي على اسم البوت
    text = message.get("text", "")
    if text and "سحابه" in text:
        return True

    # الحالة الثانية: الرسالة هي رد على رسالة سابقة من البوت
    reply = message.get("reply_to_message")
    if reply and BOT_ID:
        reply_from = reply.get("from", {})
        if reply_from.get("id") == BOT_ID:
            return True

    return False


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        raw_body = await request.body()
        logger.info(f"📥 Webhook hit | bytes={len(raw_body)}")
    except Exception as e:
        logger.error(f"❌ body read error: {e}")
        return {"ok": True}

    try:
        update = json.loads(raw_body)
    except Exception as e:
        logger.error(f"❌ JSON parse failed: {e} | raw={raw_body[:100]}")
        return {"ok": True}

    message = update.get("message", {})
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_type = chat.get("type")
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    user_id = str(message.get("from", {}).get("id", ""))
    username = message.get("from", {}).get("username", "unknown")

    logger.info(f"💬 msg | user={user_id} @{username} | chat={chat_id} type={chat_type} | text=[{text[:80]}]")

    # التحقق من صلاحية الرد في المجموعات
    if chat_type != "private":
        if not should_respond_in_group(message):
            logger.info(f"⏭️ Ignored group message (not addressed to bot nor reply)")
            return {"ok": True}

    if not chat_id or not user_id or not text:
        return {"ok": True}

    if text.startswith("/start"):
        await telegram.send_message(chat_id, "واش راك؟ أنا سحابة 🌥️\nكلمني بالعربي، الدارجة، أو حتى arabizi — أنا هنا!")
        return {"ok": True}

    await telegram.send_chat_action(chat_id)

    try:
        history = await supabase.get_history(user_id, limit=10)
        messages = history + [{"role": "user", "content": text, "thought_signature": None}]

        result = await gemini.generate_response(messages)

        await supabase.save_message(user_id, "user", text)

        metadata = {
            "tokens_used": result["tokens_used"],
            "thinking_level": result["thinking_level"],
            "model": MODEL_NAME,
            "mood": result.get("mood", "عادية"),
            "intent": result.get("intent", "سؤال"),
        }
        await supabase.save_message(
            user_id,
            "assistant",
            result["reply"],
            thought_signature=result.get("thought_signature"),
            mood=result.get("mood"),
            metadata=metadata,
        )

        await supabase.update_user(user_id, current_mood=result.get("mood"), metadata={"last_reply_tokens": result["tokens_used"]})

        await telegram.send_message(chat_id, result["reply"])

    except HTTPException as e:
        logger.error(f"❌ HTTPException {e.status_code}: {e.detail}")
        await telegram.send_message(chat_id, "عندي مشكلة تقنية دروك، جرب بعد شوية 🙏")
    except Exception as e:
        logger.error(f"❌ Webhook handler error: {type(e).__name__}: {e}", exc_info=True)
        await telegram.send_message(chat_id, "راني نحل مشكلة، عاود بعد لحظة ⚙️")

    return {"ok": True}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request = None):
    stats = orchestrator.get_stats()
    return {
        "status": "healthy" if stats["active"] > 0 else "degraded",
        "model": MODEL_NAME,
        "keys": stats,
        "rate_limits": {
            "max_rpm": MAX_RPM,
            "safe_rpm": SAFE_RPM,
            "max_tpm": MAX_TPM,
            "safe_tpm": SAFE_TPM,
            "max_rpd": MAX_RPD,
            "safe_rpd": SAFE_RPD,
        }
    }

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
    return {"status": "ok", "model": MODEL_NAME, "keys": len(GEMINI_KEYS)}

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
