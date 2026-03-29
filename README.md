# سحابة | Sahaba Bot v4.0

بوت تيليغرام يشتغل بـ **gemini-2.5-flash** — يتكلم باللهجة الجزائرية مع دمج الـ Arabizi.

---

## Stack

| Layer | Tech |
|-------|------|
| Bot API | Telegram Webhook |
| AI | `gemini-2.5-flash` |
| Backend | FastAPI + uvicorn |
| Database | Supabase (PostgreSQL) |
| Hosting | Render (Docker) |

---

## Gemini 2.5 Flash — الميزات المستغلة

> المصدر: https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash

| الميزة | التفاصيل |
|--------|----------|
| **الردود المنظمة (Structured Output)** | `{"reply": "..."}` باستخدام responseSchema |
| **thought_signatures** | تُخزّن في Supabase وتُعاد في كل جولة |
| **countTokens API** | حساب دقيق للتوكينات مع systemInstruction |
| **systemInstruction** | حقل منفصل لتوجيه شخصية البوت |
| **x-goog-api-key** | مصادقة عبر header |
| **نافذة السياق** | 1,048,576 توكين |

---

## Supabase — الجداول المطلوبة

```sql
-- جدول المحادثات
CREATE TABLE IF NOT EXISTS messages (
    id                BIGSERIAL PRIMARY KEY,
    user_id           TEXT        NOT NULL,
    role              TEXT        NOT NULL,
    content           TEXT        NOT NULL,
    thought_signature TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    mood              TEXT,
    metadata          JSONB       DEFAULT '{}'
);

-- جدول المستخدمين (للمزاج والتتبع)
CREATE TABLE IF NOT EXISTS users (
    user_id          TEXT PRIMARY KEY,
    current_mood     TEXT,
    last_interaction TIMESTAMPTZ DEFAULT now(),
    metadata         JSONB       DEFAULT '{}'
);

-- فهارس لتحسين الأداء
CREATE INDEX idx_messages_user_id_created_at ON messages(user_id, created_at DESC);
CREATE INDEX idx_users_last_interaction ON users(last_interaction DESC);
