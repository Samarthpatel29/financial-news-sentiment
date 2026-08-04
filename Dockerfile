# Hugging Face Spaces (Docker SDK) — runs the LIVE dashboard: Flask + SSE
# streaming + the pipeline scheduler, exactly like localhost. HF free tier gives
# 16 GB RAM / 2 CPU, which fits PyTorch/FinBERT comfortably.
FROM python:3.11-slim

# Build tools for lxml / native wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl && rm -rf /var/lib/apt/lists/*

# HF Spaces runs the container as uid 1000 — create a matching user with a
# writable home so the app can create data/ (SQLite) and cache the model.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

# Deps first for layer caching. CPU-only torch keeps the image ~2 GB smaller.
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# App code
COPY --chown=user . .

# Serve on the port HF Spaces expects; disable telemetry; cache the model in $HOME
ENV DASHBOARD_PORT=7860 \
    OTEL_SDK_DISABLED=true \
    CREWAI_DISABLE_TELEMETRY=1 \
    HF_HOME=/home/user/.cache/huggingface \
    TRANSFORMERS_CACHE=/home/user/.cache/huggingface \
    DATABASE_URL=sqlite:////home/user/app/data/sentiment.db

EXPOSE 7860

# run.py starts the Flask dashboard (on DASHBOARD_PORT) + the live pipeline loop.
CMD ["python", "run.py"]
