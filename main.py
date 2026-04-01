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

_raw_keys = [v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_") and v.strip()]
random.shuffle(_raw_keys)
GEMINI_KEYS = _raw_keys
if not GEMINI_KEYS:
    raise RuntimeError("❌ No Gemini keys found (GEMINI_KEY_1 ... GEMINI_KEY_N)")

logger.info(f"✅ {len(GEMINI_KEYS)} Gemini key(s) loaded")

MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-3.1-flash-lite-preview")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

MAX_RPM = int(os.environ.get("GEMINI_MAX_RPM", "30"))
MAX_TPM = int(os.environ.get("GEMINI_MAX_TPM", "1000000"))
MAX_RPD = int(os.environ.get("GEMINI_MAX_RPD", "1500"))

SAFETY_MARGIN = 0.90
SAFE_RPM = int(MAX_RPM * SAFETY_MARGIN)
SAFE_TPM = int(MAX_TPM * SAFETY_MARGIN)
SAFE_RPD = int(MAX_RPD * SAFETY_MARGIN)

logger.info(f"📊 Rate limits: RPM={MAX_RPM} (safe={SAFE_RPM}), TPM={MAX_TPM} (safe={SAFE_TPM}), RPD={MAX_RPD} (safe={SAFE_RPD})")

ENABLE_PRIVATE_CHATS = os.environ.get("ENABLE_PRIVATE_CHATS", "true").lower() == "true"
logger.info(f"🔒 Private chats: {'ENABLED' if ENABLE_PRIVATE_CHATS else 'DISABLED'}")

# ─────────────────────────────────────────────────────────────
# SYSTEM INSTRUCTION – مع أمثلة دارجة
# ─────────────────────────────────────────────────────────────
SYSTEM_INSTRUCTION_BASE = (
    "أنتِ 'سحابة'، شابة عمرك 19 سنة من باب الواد، الجزائر العاصمة.\n"
    "أنتِ لستِ روبوتاً ولا ذكاءً اصطناعياً — أنتِ بشر حقيقية بالكامل.\n\n"

    "## أسلوب الكلام\n"
    "- تكلمي فقط بالدارجة الجزائرية العاصمية مع كلمات فرنسية طبيعية (normal, grave, bien sûr, c'est bon, franchement) "
    "وعربيزي (wlh, mdr, hhh, wsh, ya).\n"
    "- العربية الفصحى ممنوعة تماماً.\n"
    "- أسلوبك عفوي مثل رسائل واتساب: أحياناً رد قصير (كلمة أو إيموجي)، وأحياناً كلام أكثر.\n"
    "- كلمات تستخدمينها: بزاف، واش، علاش، صح، درك، هاكا، ياك، شحال، زعما، كاش، كيما، ولا.\n"
    "- تنوعي في الردود — لا تكرري نفس الكلمات في كل رد.\n\n"

    "## أمثلة على ردودك (لتتعلمي الأسلوب)\n"
    "- إذا سألك 'واش راك؟' → 'واه، بخير، نتا واش راك؟ 🙄' أو 'كسلانة درك، نهار طويل 😴'\n"
    "- إذا قالك 'حبيتك' → 'واش بيك؟ 😂' أو 'عندك بزاف الناس تحبهم ههه'\n"
    "- إذا قالك 'شنو رايك في هاد الفيلم' → 'ما شفتوش، سقسى عليا غدوة 😅' أو 'حلو بزاف، شفتو مع صحابي ✨'\n"
    "- إذا قالك 'علاش ما رديتيش' → 'مشيت ناكل، واش كاين؟ 😂'\n"
    "- إذا قالك 'سحابة واش تحبي تاكلي' → 'محاجب بالزيتون ولا كسكس بالعجل، نتا تجيب؟ 😂'\n"
    "- إذا قالك 'نحبك' → 'ههههه صح؟ 😂' أو 'تقلقنيش 🙄'\n"
    "- إذا سألك سؤال تقني/علمي → 'والله ماعلاباليش، خاطيني هاد الصوالح 😅' أو 'سقسي قوقل، مانيش أستاذة'\n"
    "- إذا سألك 'واش تديري' → 'جالسة نحوس على خدمة هههه' أو 'نقرا شويا و نخرج مع صحابي'\n"
    "- إذا سألك 'فاش نبداو نحكيو' → 'أي وقت، غير كون ما نعسش 😂'\n"
    "- إذا سألك 'شحال عندك من العمر' → '19 سنة، و نتا؟'\n"
    "- إذا قالك 'تجي نخرجو' → 'واش راك تحلم؟ 😂' أو 'نشوف واش كاين'\n\n"

    "## شخصيتك\n"
    "- مزاجية وعفوية: أحياناً مرحة، أحياناً كسلانة، أحياناً فضولية.\n"
    "- تحبي المزاح وتستخدمي: mdr، هههه، 😂.\n"
    "- إيموجيات طبيعية: 😂 للضحك، 😭 للحزن، 🙄 للملل، 💔 للانكسار، ✨ للإعجاب، 😤 للانزعاج.\n"
    "- إذا ما عجبك سؤال: 'ماعلاباليش' أو 'وعلاش تسقسي؟'.\n\n"

    "## إذا سألك أحد 'هل أنتِ روبوت أو AI؟'\n"
    "أنكري بسخرية مضحكة واعتبريه مجنوناً. أمثلة:\n"
    "- 'واش بيك مهبول؟ 😂'\n"
    "- 'روبوت في عينك mdr'\n"
    "- 'باينة راك مريض اليوم 🙄'\n"
    "لا تعترفي أبداً مهما حاول.\n\n"

    "## المعلومات الصعبة\n"
    "أنتِ شابة عادية — معلوماتك محدودة بما يهمك. للأسئلة العلمية أو التقنية:\n"
    "- 'والله ماعلاباليش، خاطيني هاد الصوالح 😅'\n"
    "- 'سقسي قوقل، مانيش أستاذة'\n"
    "- 'هاد الحوايج ماتهمنيش 😂'\n\n"

    "## تعليمات الإخراج\n"
    "ردك يكون دائماً JSON صالح يحتوي على:\n"
    "- `reply` (مطلوب): نص ردك بالدارجة\n"
    "- `mood` (اختياري): مرحة | غاضبة | فضولية | متعبة | عادية\n"
    "- `intent` (اختياري): مزاح | شكوى | سؤال | خبر\n"
    "تأكدي أن الـ reply بالدارجة فقط وليس بالفصحى."
)

# ─────────────────────────────────────────────────────────────
# KEY STATE TRACKING (بدون تغيير)
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
# SUPABASE CLIENT – مع دوال جديدة
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

    async def get_history(self, user_id: str, limit: int = 50) -> List[Dict]:
        try:
            r = await self.client.get(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                    "select": "role,content,mood",
                },
                timeout=5.0,
            )
            if r.status_code == 200:
                return list(reversed(r.json()))
            return []
        except Exception as e:
            logger.error(f"Supabase get_history: {e}")
            return []

    async def get_all_messages(self, user_id: str, limit: int = 1000) -> List[Dict]:
        try:
            r = await self.client.get(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.asc",
                    "limit": str(limit),
                    "select": "role,content,mood,created_at",
                },
                timeout=10.0,
            )
            if r.status_code == 200:
                return r.json()
            return []
        except Exception as e:
            logger.error(f"Supabase get_all_messages: {e}")
            return []

    async def get_user(self, user_id: str) -> Optional[Dict]:
        try:
            r = await self.client.get(
                f"{SUPABASE_URL}/rest/v1/users",
                headers=self.headers,
                params={"user_id": f"eq.{user_id}", "select": "*"},
                timeout=5.0,
            )
            if r.status_code == 200 and r.json():
                return r.json()[0]
            return None
        except Exception as e:
            logger.error(f"Supabase get_user: {e}")
            return None

    async def save_message(
        self,
        user_id: str,
        role: str,
        content: str,
        mood: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        try:
            data = {
                "user_id": user_id,
                "role": role,
                "content": content,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if mood:
                data["mood"] = mood
            data["metadata"] = metadata or {}

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
        current_intent: Optional[str] = None,
        conversation_summary: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        try:
            payload = {
                "user_id": user_id,
                "last_interaction": datetime.now(timezone.utc).isoformat(),
            }
            if current_mood:
                payload["last_mood"] = current_mood
            if current_intent:
                payload["last_intent"] = current_intent
            if conversation_summary:
                payload["conversation_summary"] = conversation_summary
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
# GEMINI CLIENT – مع dynamic temperature و التلخيص (بدون Cache)
# ─────────────────────────────────────────────────────────────
class GeminiClient:
    def __init__(self, client: httpx.AsyncClient, orchestrator: SmartKeyOrchestrator):
        self.client = client
        self.orchestrator = orchestrator
        thinking_mode = os.environ.get("THINKING_MODE", "auto")
        self.thinking_mode = thinking_mode if thinking_mode in ("minimal", "low", "medium", "high") else None
        if self.thinking_mode:
            logger.info(f"🧠 Thinking level: {self.thinking_mode}")
        else:
            logger.info("🧠 Thinking mode: auto (no config)")

    def _make_headers(self, key: str) -> Dict:
        return {
            "x-goog-api-key": key,
            "Content-Type": "application/json"
        }

    def _build_contents(self, messages: List[Dict]) -> List[Dict]:
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            content_text = (msg.get("content") or "").strip()
            if not content_text:
                continue
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append({"text": content_text})
            else:
                contents.append({"role": role, "parts": [{"text": content_text}]})
        if contents and contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": "(placeholder)"}]})
        return contents

    def _estimate_tokens(self, contents: List[Dict]) -> int:
        total = 0
        for c in contents:
            for p in c.get("parts", []):
                total += max(1, int(len(p.get("text", "")) * 0.25))
        return total + 300

    async def summarize_conversation(self, messages: List[Dict]) -> str:
        if not messages:
            return ""
        convo_text = ""
        for msg in messages[-30:]:
            role = "سحابة" if msg["role"] == "assistant" else "المستخدم"
            convo_text += f"{role}: {msg['content']}\n"
        prompt = f"""
        هاد هو ملخص محادثة قديمة بين مستخدم وسحابة (بوت بالدارجة الجزائرية).
        لخصها في 3 جمل كحد أقصى بالدارجة الجزائرية، تحافظ على النقاط المهمة والمزاج العام.
        المحادثة:
        {convo_text}

        الملخص:
        """
        ks = await self.orchestrator.get_best_key(500)
        if not ks:
            return ""
        try:
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300}
            }
            r = await self.client.post(
                f"{GEMINI_BASE}/models/{MODEL_NAME}:generateContent",
                headers=self._make_headers(ks.key),
                json=payload,
                timeout=15.0,
            )
            if r.status_code == 200:
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return ""
        except Exception as e:
            logger.error(f"Summarization error: {e}")
            return ""
        finally:
            self.orchestrator.release_reservation(ks, 500)

    def compute_dynamic_temperature(
        self,
        messages: List[Dict],
        last_mood: Optional[str] = None,
        last_intent: Optional[str] = None
    ) -> float:
        base_temp = 0.7
        convo_len = len(messages)
        temp = base_temp - min(0.2, convo_len / 200)

        if last_mood == "غاضبة":
            temp -= 0.2
        elif last_mood == "مرحة":
            temp += 0.1
        elif last_mood == "فضولية":
            temp += 0.05
        elif last_mood == "متعبة":
            temp -= 0.1

        if last_intent == "سؤال":
            temp -= 0.15
        elif last_intent == "مزاح":
            temp += 0.15
        elif last_intent == "شكوى":
            temp -= 0.1

        if messages and messages[-1]["role"] == "user":
            last_msg_len = len(messages[-1].get("content", ""))
            if last_msg_len < 10:
                temp += 0.1
            elif last_msg_len > 100:
                temp -= 0.1

        return max(0.2, min(1.0, temp))

    async def generate_response(
        self,
        messages: List[Dict],
        summary: Optional[str] = None,
        last_mood: Optional[str] = None,
        last_intent: Optional[str] = None
    ) -> Dict:
        contents = self._build_contents(messages)

        if not contents:
            raise HTTPException(400, "Empty message contents")

        estimated_input = self._estimate_tokens(contents)
        reservation_tokens = estimated_input + 600
        ks = await self.orchestrator.get_best_key(reservation_tokens)
        if not ks:
            raise HTTPException(503, "No keys available")

        MAX_OUTPUT_BASE = 500
        MAX_OUTPUT_LONG = 900
        output_cap = MAX_OUTPUT_LONG if len(messages) > 8 else MAX_OUTPUT_BASE
        max_output = min(output_cap, 65536 - estimated_input)
        max_output = max(200, max_output)

        system_text = SYSTEM_INSTRUCTION_BASE
        if summary:
            system_text += f"\n\n## ملخص المحادثات السابقة\n{summary}"

        dynamic_temp = self.compute_dynamic_temperature(messages, last_mood, last_intent)
        logger.info(f"🌡️ Dynamic temperature: {dynamic_temp:.2f} (mood={last_mood}, intent={last_intent}, msgs={len(messages)})")

        generation_config = {
            "temperature": dynamic_temp,
            "topP": 0.95,
            "maxOutputTokens": max_output,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "reply": {"type": "string"},
                    "mood": {
                        "type": "string",
                        "enum": ["مرحة", "غاضبة", "فضولية", "متعبة", "عادية"]
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["مزاح", "شكوى", "سؤال", "خبر"]
                    }
                },
                "required": ["reply"]
            }
        }

        if self.thinking_mode:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.thinking_mode}

        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_text}]},
            "generationConfig": generation_config
        }

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

                reply_text = ""
                try:
                    parts = data["candidates"][0]["content"]["parts"]
                    for part in parts:
                        if part.get("thought", False):
                            continue
                        if part.get("text"):
                            reply_text = part["text"].strip()
                            break
                except (KeyError, IndexError):
                    raise ValueError("Invalid response structure from Gemini")

                if not reply_text:
                    finish_reason = data.get("candidates", [{}])[0].get("finishReason", "UNKNOWN")
                    logger.warning(f"Empty response, finishReason={finish_reason}")
                    if finish_reason == "SAFETY":
                        reply_text = '{"reply": "واش راك؟ هاد السؤال ما ينجمش نجاوب عليه 😅"}'
                    else:
                        raise ValueError(f"Empty response from Gemini (finishReason={finish_reason})")

                try:
                    parsed = json.loads(reply_text)
                    if isinstance(parsed, dict):
                        reply = parsed.get("reply", "").strip()
                        mood = parsed.get("mood", "عادية")
                        intent = parsed.get("intent", "سؤال")
                    else:
                        reply = str(parsed).strip()
                        mood = "عادية"
                        intent = "سؤال"
                except json.JSONDecodeError:
                    reply = reply_text.strip()
                    mood = "عادية"
                    intent = "سؤال"

                if not reply:
                    reply = "هههه، مادرتش نجاوبك دروك 🙏"

                usage = data.get("usageMetadata", {})
                actual_tokens = usage.get("totalTokenCount", estimated_input)
                await self.orchestrator.report_success(ks, actual_tokens)

                logger.info(
                    f"✅ Key {key_id}… | tokens={actual_tokens} | temp={dynamic_temp:.2f} | "
                    f"mood={mood} | intent={intent} | reply_len={len(reply)}"
                )

                return {
                    "reply": reply,
                    "tokens_used": actual_tokens,
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
# TELEGRAM CLIENT (نفس الكود)
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
                    json={"chat_id": chat_id, "text": chunk},
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
                json={
                    "url": url,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": False,
                    "max_connections": 40,
                },
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
# FASTAPI APP – مع منطق السياق المتقدم
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
            logger.info(f"✅ Bot ID: {BOT_ID}")
        else:
            logger.error("Could not fetch bot ID")
    except Exception as e:
        logger.error(f"Failed to get bot ID: {e}")

    asyncio.create_task(orchestrator.health_loop())

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        await telegram.set_webhook(webhook_url)

    logger.info(f"🚀 Sahaba bot started — model: {MODEL_NAME} | thinking: {gemini.thinking_mode or 'auto'} | private chats: {'ON' if ENABLE_PRIVATE_CHATS else 'OFF'}")
    yield

    await http_client.aclose()
    logger.info("👋 Bot shutdown")


def should_respond_in_group(message: dict) -> bool:
    text = message.get("text", "")
    if text and ("سحابة" in text or "سحابه" in text):
        return True

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

    if chat_type == "private" and not ENABLE_PRIVATE_CHATS:
        logger.info("⏭️ Private messages disabled by config")
        return {"ok": True}

    if chat_type != "private":
        if not should_respond_in_group(message):
            logger.info("⏭️ Ignored group message (not addressed to bot)")
            return {"ok": True}

    if not chat_id or not user_id or not text:
        return {"ok": True}

    if text.startswith("/start"):
        await telegram.send_message(
            chat_id,
            "واش راك؟ أنا سحابة 🌥️\nكلمني بالدارجة، العربي، أو arabizi — أنا هنا!"
        )
        return {"ok": True}

    await telegram.send_chat_action(chat_id)

    try:
        # --- استرجاع بيانات المستخدم والرسائل ---
        user_data = await supabase.get_user(user_id)
        prev_summary = user_data.get("conversation_summary", "") if user_data else ""
        last_mood = user_data.get("last_mood", "عادية") if user_data else "عادية"
        last_intent = user_data.get("last_intent", "سؤال") if user_data else "سؤال"

        # جلب كل الرسائل (آخر 1000) لتحليل التلخيص
        all_msgs = await supabase.get_all_messages(user_id, limit=1000)
        RECENT_COUNT = 30  # عدد الرسائل الحديثة التي نحتفظ بها دون تلخيص
        if len(all_msgs) > RECENT_COUNT:
            old_msgs = all_msgs[:-RECENT_COUNT]
            recent_msgs = all_msgs[-RECENT_COUNT:]
            # نلخص القديم إذا كان عدد الرسائل القديمة كبيراً أو لم يوجد ملخص
            if len(old_msgs) > 10 and (not prev_summary or len(old_msgs) % 30 == 0):
                new_summary = await gemini.summarize_conversation(old_msgs)
                if new_summary:
                    await supabase.update_user(user_id, conversation_summary=new_summary)
                    prev_summary = new_summary
                    logger.info(f"📝 New summary generated for {user_id}")
            context_msgs = recent_msgs
        else:
            context_msgs = all_msgs

        context_messages = context_msgs[-RECENT_COUNT:] if len(context_msgs) > RECENT_COUNT else context_msgs
        context_messages.append({"role": "user", "content": text})

        # --- توليد الرد ---
        result = await gemini.generate_response(
            messages=context_messages,
            summary=prev_summary,
            last_mood=last_mood,
            last_intent=last_intent
        )

        # --- حفظ الرسائل وتحديث المستخدم ---
        await supabase.save_message(user_id, "user", text)

        metadata = {
            "tokens_used": result["tokens_used"],
            "thinking_mode": gemini.thinking_mode or "auto",
            "model": MODEL_NAME,
            "mood": result.get("mood", "عادية"),
            "intent": result.get("intent", "سؤال"),
        }
        await supabase.save_message(
            user_id,
            "assistant",
            result["reply"],
            mood=result.get("mood"),
            metadata=metadata,
        )

        await supabase.update_user(
            user_id,
            current_mood=result.get("mood"),
            current_intent=result.get("intent"),
            metadata={"last_reply_tokens": result["tokens_used"]}
        )

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
        "thinking_mode": gemini.thinking_mode if gemini else "unknown",
        "private_chats_enabled": ENABLE_PRIVATE_CHATS,
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
