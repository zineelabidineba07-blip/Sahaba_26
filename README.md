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
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token من @BotFather |
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_KEY` | service_role key |
| `RENDER_URL` | `https://sahaba-bot.onrender.com` |
| `WEBHOOK_SECRET` | يتولد تلقائياً من Render |
| `GEMINI_KEY_1` … `GEMINI_KEY_N` | مفاتيح Gemini API |

---

## Deploy على Render

1. ارفع الكود على GitHub
2. اذهب إلى [render.com](https://render.com) → New Web Service → Connect GitHub
3. Render يكتشف `render.yaml` تلقائياً
4. أضف env variables في الـ dashboard
5. Deploy ✅

بعد أول deploy، الـ webhook يتسجل تلقائياً مع Telegram.

---

## Rate Limits — gemini-3-flash-preview

> المصدر: https://ai.google.dev/gemini-api/docs/rate-limits
> Preview models use paid tier limits. Check your actual quotas at:
> https://aistudio.google.com/rate-limit

القيم في الكود محافظة (90% safety margin). عدّلها بعد ما تشوف quotas حسابك.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/webhook/{secret}` | Telegram updates |
| GET | `/health` | Health check |
| GET | `/keys/status` | Key orchestrator stats |
| GET | `/` | Bot info |
