# سحابة | Sahaba Bot v4.0

بوت تيليغرام يشتغل بـ **gemini-3-flash-preview** — يتكلم بالدارجة الجزائرية.

---

## Stack

| Layer | Tech |
|---|---|
| Bot API | Telegram Webhook |
| AI | `gemini-3-flash-preview` |
| Backend | FastAPI + uvicorn |
| Database | Supabase (PostgreSQL) |
| Hosting | Render (Docker) |

---

## Gemini 3 Flash — ميزات مستغلة كاملاً

> المصدر: https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview

| الميزة | التفاصيل |
|---|---|
| **thinking_level** | dynamic: minimal → medium حسب طول المحادثة |
| **thought_signatures** | تُخزّن في Supabase وتُعاد في كل turn |
| **countTokens API** | يشمل systemInstruction — دقة 100% |
| **responseSchema** | `{"reply": "..."}` — مضمون دائماً |
| **systemInstruction** | field منفصل (top-level) كما تنص الوثائق |
| **x-goog-api-key** | header authentication (Gemini 3 recommended) |
| **1M context window** | تاريخ 20 رسالة مع thought signatures |

---

## Supabase — جدول المطلوب

```sql
create table messages (
  id               bigserial primary key,
  user_id          text        not null,
  role             text        not null,  -- 'user' | 'assistant'
  content          text        not null,
  thought_signature text,                 -- Gemini 3 thought signatures
  created_at       timestamptz not null default now()
);

create index on messages (user_id, created_at desc);
