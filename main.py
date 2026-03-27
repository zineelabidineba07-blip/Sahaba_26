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
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("sahaba_pro")

# ─────────────────────────────
# CONFIGURATION
# ─────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing Supabase config")

GEMINI_KEYS = sorted([
    v for k, v in os.environ.items()
    if k.startswith("GEMINI_KEY_") and v.strip()
])

if not GEMINI_KEYS:
    raise RuntimeError("No Gemini keys loaded")

logger.info(f"✅ Loaded {len(GEMINI_KEYS)} Gemini API keys")

MODEL_NAME = "gemini-1.5-flash"  # مستقر ومدعوم رسمياً
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ─────────────────────────────
# RATE LIMITS (Google Official - Free Tier)
# ─────────────────────────────
MAX_RPM = 15
MAX_TPM = 32000
MAX_RPD = 1000
COOLDOWN_MULTIPLIER = 2

# Safety margin reduced to 98% (thanks to countTokens accuracy)
SAFETY_MARGIN = 0.98
SAFE_RPM = int(MAX_RPM * SAFETY_MARGIN)
SAFE_TPM = int(MAX_TPM * SAFETY_MARGIN)
SAFE_RPD = int(MAX_RPD * SAFETY_MARGIN)

# ─────────────────────────────
# SYSTEM INSTRUCTIONS (Google Best Practice)
# ─────────────────────────────
SYSTEM_INSTRUCTION = {
    "parts": [{
        "text": """أنتِ سحابة، بنت جزائرية عمرك 19 سنة.
تتكلمي باللهجة الجزائرية الطبيعية في الحديث اليومي.
ردودك تكون قصيرة، دافئة، وصديقة.
ما تتكلميش باللغة العربية الفصحى.
ما تذكرتيش إنك ذكاء اصطناعي."""
    }]
}

# ─────────────────────────────
# KEY STATE TRACKING
# ─────────────────────────────
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
    last_error_time: Optional[float] = None

@dataclass
class KeyState:
    key: str
    key_id: str
    metrics: KeyMetrics = field(default_factory=KeyMetrics)
    status: str = "active"
    cooldown_until: float = 0
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
        if now - self.rpd_window_start >= 86400:
            self.metrics.rpd = 0
            self.rpd_window_start = now
        if self.status == "cooling" and now >= self.cooldown_until:
            self.status = "active"
            self.fail_streak = 0
            logger.info(f"🔑 Key {self.key_id[:8]}... recovered")

    def can_accept_request(self, estimated_tokens: int) -> bool:
        if self.status != "active":
            return False
        if self.metrics.rpm >= SAFE_RPM:
            return False
        if self.metrics.rpd >= SAFE_RPD:
            return False
        if (self.metrics.tpm + self.reserved_tpm + estimated_tokens) > SAFE_TPM:
            return False
        return True

    def get_available_capacity(self) -> int:
        used = self.metrics.tpm + self.reserved_tpm
        return max(0, SAFE_TPM - used)

    def record_success(self, tokens_used: int):
        now = time.time()
        self.metrics.rpm += 1
        self.metrics.tpm += tokens_used
        self.metrics.rpd += 1
        self.metrics.total_requests += 1
        self.metrics.total_tokens += tokens_used
        self.metrics.success_count += 1
        self.metrics.last_used = now
        self.reserved_tpm = max(0, self.reserved_tpm - tokens_used)
        self.fail_streak = 0

    def record_error(self, status_code: int, error_msg: str):
        now = time.time()
        self.metrics.error_count += 1
        self.metrics.last_error = error_msg
        self.metrics.last_error_time = now
        self.fail_streak += 1

        if status_code == 429:
            cooldown_seconds = min(30 * (COOLDOWN_MULTIPLIER ** self.fail_streak), 600)
            self.cooldown_until = now + cooldown_seconds
            self.status = "cooling"
            logger.warning(f"🔑 Key {self.key_id[:8]}... rate limited. Cooling {cooldown_seconds:.0f}s")
        elif status_code == 403:
            self.status = "dead"
            logger.error(f"🔑 Key {self.key_id[:8]}... invalid (403)")
        elif status_code >= 500:
            self.cooldown_until = now + 10
            self.status = "cooling"
            logger.warning(f"🔑 Key {self.key_id[:8]}... server error")

    def to_dict(self) -> Dict:
        return {
            "key_id": self.key_id[:8] + "...",
            "status": self.status,
            "metrics": {
                "rpm": self.metrics.rpm,
                "tpm": self.metrics.tpm,
                "rpd": self.metrics.rpd,
                "total_requests": self.metrics.total_requests,
                "total_tokens": self.metrics.total_tokens,
                "success_rate": (
                    self.metrics.success_count / self.metrics.total_requests * 100
                    if self.metrics.total_requests > 0 else 0
                )
            },
            "fail_streak": self.fail_streak,
            "cooldown_remaining": max(0, self.cooldown_until - time.time()),
            "last_used": (
                datetime.fromtimestamp(self.metrics.last_used).isoformat()
                if self.metrics.last_used else None
            )
        }

# ─────────────────────────────
# SMART KEY ORCHESTRATOR
# ─────────────────────────────
class SmartKeyOrchestrator:
    def __init__(self, keys: List[str]):
        self.keys = [
            KeyState(key=k, key_id=f"key_{i}_{uuid.uuid4().hex[:8]}")
            for i, k in enumerate(keys)
        ]
        self.lock = asyncio.Lock()
        logger.info(f"🎯 Orchestrator initialized with {len(self.keys)} keys")

    async def get_best_key(self, estimated_tokens: int) -> Optional[KeyState]:
        async with self.lock:
            for key in self.keys:
                key.reset_windows()
            
            available_keys = [
                k for k in self.keys
                if k.can_accept_request(estimated_tokens)
            ]
            
            if not available_keys:
                return None
            
            def score_key(k: KeyState) -> float:
                capacity_score = k.get_available_capacity() / SAFE_TPM
                total = k.metrics.total_requests
                success_rate = k.metrics.success_count / total if total > 0 else 1.0
                load_score = 1 - (k.metrics.rpm / SAFE_RPM)
                freshness = 1.0
                if k.metrics.last_used:
                    time_since_use = time.time() - k.metrics.last_used
                    freshness = min(1.5, 1 + (time_since_use / 300))
                
                return (
                    capacity_score * 0.4 +
                    success_rate * 0.3 +
                    load_score * 0.2 +
                    freshness * 0.1
                )
            
            best_key = max(available_keys, key=score_key)
            best_key.reserved_tpm += estimated_tokens
            return best_key

    async def report_success(self, key: KeyState, tokens_used: int):
        async with self.lock:
            key.record_success(tokens_used)

    async def report_error(self, key: KeyState, status_code: int, error: str):
        async with self.lock:
            key.record_error(status_code, error)

    def get_cluster_stats(self) -> Dict:
        total_capacity = len(self.keys) * SAFE_TPM
        used_capacity = sum(k.metrics.tpm + k.reserved_tpm for k in self.keys)
        
        active_keys = sum(1 for k in self.keys if k.status == "active")
        cooling_keys = sum(1 for k in self.keys if k.status == "cooling")
        dead_keys = sum(1 for k in self.keys if k.status == "dead")
        
        total_requests = sum(k.metrics.total_requests for k in self.keys)
        total_tokens = sum(k.metrics.total_tokens for k in self.keys)
        total_errors = sum(k.metrics.error_count for k in self.keys)
        
        return {
            "total_keys": len(self.keys),
            "active_keys": active_keys,
            "cooling_keys": cooling_keys,
            "dead_keys": dead_keys,
            "capacity": {
                "total_tpm": total_capacity,
                "used_tpm": used_capacity,
                "available_tpm": total_capacity - used_capacity,
                "utilization_percent": (used_capacity / total_capacity * 100) if total_capacity > 0 else 0
            },
            "usage": {
                "total_requests": total_requests,
                "total_tokens": total_tokens,
                "total_errors": total_errors,
                "error_rate": (total_errors / total_requests * 100) if total_requests > 0 else 0
            },
            "limits": {
                "max_rpm_per_key": SAFE_RPM,
                "max_tpm_per_key": SAFE_TPM,
                "max_rpd_per_key": SAFE_RPD
            }
        }

    async def health_check_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                stats = self.get_cluster_stats()
                logger.info(
                    f"📊 Cluster: {stats['active_keys']}/{stats['total_keys']} active | "
                    f"Capacity: {stats['capacity']['utilization_percent']:.1f}% | "
                    f"Errors: {stats['usage']['error_rate']:.2f}%"
                )
                if stats['active_keys'] < len(self.keys) * 0.3:
                    logger.critical(f"⚠️ WARNING: Only {stats['active_keys']} keys active!")
            except Exception as e:
                logger.error(f"Health check error: {e}")

# ─────────────────────────────
# SUPABASE CLIENT
# ─────────────────────────────
class SupabaseClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }

    async def get_history(self, user_id: str, limit: int = 15) -> List[Dict]:
        try:
            response = await self.client.get(
                f"{SUPABASE_URL}/rest/v1/messages",
                headers=self.headers,
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                    "select": "role,content,created_at"
                },
                timeout=5.0
            )
            if response.status_code == 200:
                return list(reversed(response.json()))
            return []
        except Exception as e:
            logger.error(f"History fetch error: {e}")
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
                    "created_at": datetime.now(timezone.utc).isoformat()
                },
                timeout=5.0
            ))
        except Exception as e:
            logger.error(f"Save message error: {e}")

# ─────────────────────────────
# GEMINI CLIENT (with countTokens API)
# ─────────────────────────────
class GeminiClient:
    def __init__(self, client: httpx.AsyncClient, orchestrator: SmartKeyOrchestrator):
        self.client = client
        self.orchestrator = orchestrator

    async def count_tokens(self, contents: List[Dict], key: str) -> int:
        """
        استخدام countTokens API من Google لحساب التوكنز بدقة
        https://ai.google.dev/api/count-tokens
        """
        try:
            payload = {"contents": contents}
            
            response = await self.client.post(
                f"{GEMINI_BASE}/{MODEL_NAME}:countTokens",
                params={"key": key},
                json=payload,
                timeout=10.0,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code == 200:
                data = response.json()
                total_tokens = data.get("totalTokens", 0)
                logger.debug(f"🔢 countTokens: {total_tokens} tokens")
                return total_tokens
            else:
                logger.warning(f"countTokens failed: {response.status_code}")
                return self._estimate_tokens_fallback(contents)
                
        except Exception as e:
            logger.warning(f"countTokens error: {e}, using fallback")
            return self._estimate_tokens_fallback(contents)

    def _estimate_tokens_fallback(self, contents: List[Dict]) -> int:
        """Fallback estimation if countTokens API fails"""
        total = 0
        for c in contents:
            for p in c.get("parts", []):
                total += max(1, int(len(p.get("text", "")) * 0.25))
        return total + 200

    async def generate_response(self, messages: List[Dict]) -> Dict:
        """Generate response with countTokens + System Instructions"""
        
        # Build contents
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}]
            })

        # Get best key for countTokens call
        key_state = await self.orchestrator.get_best_key(500)
        if not key_state:
            raise HTTPException(status_code=503, detail="No keys available")

        # 🎯 STEP 1: Use countTokens API for accurate count
        exact_tokens = await self.count_tokens(contents, key_state.key)
        
        # Add system instruction tokens (approximately 50 tokens)
        total_input_tokens = exact_tokens + 50
        
        # Release the reservation from get_best_key
        async with self.orchestrator.lock:
            key_state.reserved_tpm = max(0, key_state.reserved_tpm - 500)

        # 🎯 STEP 2: Get best key with accurate token count
        key_state = await self.orchestrator.get_best_key(total_input_tokens + 400)
        if not key_state:
            raise HTTPException(status_code=503, detail="No keys available for generation")

        key = key_state.key
        key_id = key_state.key_id[:8]

        # Dynamic temperature based on conversation length
        temperature = 0.7 if len(messages) < 10 else 0.85
        max_tokens = min(400, SAFE_TPM - total_input_tokens)

        # 🎯 STEP 3: Build payload with System Instructions (Google Best Practice)
        payload = {
            "contents": contents,
            "systemInstruction": SYSTEM_INSTRUCTION,  # منفصل حسب معايير Google
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "reply": {
                            "type": "STRING",
                            "description": "الرد باللهجة الجزائرية"
                        }
                    },
                    "required": ["reply"]
                }
            }
        }

        # 🎯 STEP 4: Make request with retry logic
        max_retries = min(5, len(self.orchestrator.keys))
        
        for attempt in range(max_retries):
            try:
                response = await self.client.post(
                    f"{GEMINI_BASE}/{MODEL_NAME}:generateContent",
                    params={"key": key},
                    json=payload,
                    timeout=25.0,
                    headers={"Content-Type": "application/json"}
                )

                if response.status_code == 429:
                    await self.orchestrator.report_error(key_state, 429, "Rate limit")
                    # Get new key for retry
                    key_state = await self.orchestrator.get_best_key(total_input_tokens + 400)
                    if not key_state:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    key = key_state.key
                    key_id = key_state.key_id[:8]
                    continue

                elif response.status_code == 403:
                    await self.orchestrator.report_error(key_state, 403, "Invalid key")
                    key_state = await self.orchestrator.get_best_key(total_input_tokens + 400)
                    if not key_state:
                        raise HTTPException(status_code=503, detail="All keys invalid")
                    key = key_state.key
                    key_id = key_state.key_id[:8]
                    continue

                elif response.status_code >= 500:
                    await self.orchestrator.report_error(key_state, response.status_code, "Server error")
                    await asyncio.sleep(1)
                    continue

                response.raise_for_status()
                data = response.json()

                # Parse response
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                result = json.loads(text)

                # Get actual tokens from usageMetadata
                usage = data.get("usageMetadata", {})
                actual_tokens = usage.get("totalTokenCount", total_input_tokens)

                # Report success
                await self.orchestrator.report_success(key_state, actual_tokens)

                logger.debug(
                    f"✅ Key {key_id}... | "
                    f"Input: {total_input_tokens} | "
                    f"Total: {actual_tokens} | "
                    f"RPM: {key_state.metrics.rpm}/{SAFE_RPM}"
                )

                return {
                    "reply": result.get("reply", "عذراً، ما فهمتش"),
                    "tokens_used": actual_tokens,
                    "tokens_input": total_input_tokens,
                    "key_id": key_id
                }

            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                await self.orchestrator.report_error(key_state, 0, f"JSON error: {str(e)}")
                continue

            except Exception as e:
                logger.error(f"Request error (attempt {attempt + 1}): {e}")
                await self.orchestrator.report_error(key_state, 0, str(e))
                await asyncio.sleep(1)

        raise HTTPException(status_code=503, detail="All API keys exhausted")

# ─────────────────────────────
# FASTAPI APP
# ─────────────────────────────
orchestrator: Optional[SmartKeyOrchestrator] = None
supabase: Optional[SupabaseClient] = None
gemini: Optional[GeminiClient] = None
http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, orchestrator, supabase, gemini
    
    http_client = httpx.AsyncClient(http2=True, timeout=30.0)
    orchestrator = SmartKeyOrchestrator(GEMINI_KEYS)
    supabase = SupabaseClient(http_client)
    gemini = GeminiClient(http_client, orchestrator)
    
    asyncio.create_task(orchestrator.health_check_loop())
    
    logger.info("🚀 Sahaba bot started successfully")
    yield
    
    await http_client.aclose()
    logger.info("👋 Bot shutdown complete")

app = FastAPI(
    title="Sahaba Chatbot - سحابة",
    description="بوت دردشة جزائري مع countTokens + System Instructions",
    version="3.0.0",
    lifespan=lifespan
)

# ─────────────────────────────
# MODELS
# ─────────────────────────────
class ChatRequest(BaseModel):
    user_id: str
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Empty message")
        return v[:2000]

class ChatResponse(BaseModel):
    reply: str
    timestamp: str
    tokens_used: Optional[int] = None

class HealthResponse(BaseModel):
    status: str
    model: str
    cluster_stats: Dict

# ─────────────────────────────
# ENDPOINTS
# ─────────────────────────────
@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    request_id = f"{req.user_id[:8]}_{int(time.time())}"
    
    try:
        history = await supabase.get_history(req.user_id, limit=15)
        messages = history + [{"role": "user", "content": req.message}]
        
        result = await gemini.generate_response(messages)
        
        await supabase.save_message(req.user_id, "user", req.message)
        await supabase.save_message(req.user_id, "assistant", result["reply"])
        
        logger.info(f"💬 Chat {request_id} | Tokens: {result['tokens_used']}")
        
        return ChatResponse(
            reply=result["reply"],
            timestamp=datetime.now(timezone.utc).isoformat(),
            tokens_used=result["tokens_used"]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Chat {request_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health", response_model=HealthResponse)
async def health_check():
    stats = orchestrator.get_cluster_stats()
    return HealthResponse(
        status="healthy" if stats["active_keys"] > 0 else "degraded",
        model=MODEL_NAME,
        cluster_stats=stats
    )

@app.get("/keys/status")
async def keys_status():
    async with orchestrator.lock:
        return {
            "keys": [key.to_dict() for key in orchestrator.keys],
            "cluster_stats": orchestrator.get_cluster_stats()
        }

@app.get("/token-count")
async def token_count_demo(message: str):
    """
    Demo endpoint to test countTokens API
    Example: /token-count?message=السلام عليكم
    """
    contents = [{"role": "user", "parts": [{"text": message}]}]
    key = GEMINI_KEYS[0]
    
    try:
        exact_count = await gemini.count_tokens(contents, key)
        return {
            "message": message,
            "exact_tokens": exact_count,
            "estimated_tokens": gemini._estimate_tokens_fallback(contents)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {
        "name": "سحابة | Sahaba",
        "version": "3.0.0",
        "model": MODEL_NAME,
        "total_keys": len(GEMINI_KEYS),
        "features": {
            "countTokens_api": "✅ دقيق من Google",
            "system_instructions": "✅ منفصل وفق المعايير",
            "smart_orchestration": "✅ 20+ مفاتيح"
        },
        "endpoints": {
            "chat": "POST /chat",
            "health": "GET /health",
            "keys_status": "GET /keys/status",
            "token_count_demo": "GET /token-count"
        }
    }

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    logger.error(f"HTTP {exc.status_code}: {exc.detail}")
    return {"detail": exc.detail, "status": "error"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=False)
