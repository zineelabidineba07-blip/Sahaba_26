FROM python:3.11-slim

# ── Environment ──────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── Security: non-root user ───────────────────────────────────
RUN useradd -m -u 1000 appuser
WORKDIR /app

# ── Dependencies ──────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App code ──────────────────────────────────────────────────
COPY . .
RUN chown -R appuser:appuser /app

USER appuser

# ── Healthcheck ───────────────────────────────────────────────
# Calls the /health endpoint that FastAPI exposes.
# Render will also use the HTTP health check configured in render.yaml.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c \
      "import httpx; r = httpx.get('http://localhost:7860/health', timeout=8); exit(0 if r.status_code == 200 else 1)"

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
