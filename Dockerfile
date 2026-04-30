# Vera Bot — works on Railway, HuggingFace Spaces, Render, Fly.io
# (any platform that runs a Docker container and sets $PORT)
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app.py composer.py prompts.py validator.py state.py llm.py /app/

# Default to 7860 (HF Spaces convention) if $PORT not set (Railway sets it dynamically)
ENV PORT=7860
EXPOSE 7860

# HF Spaces sets up a non-root user; make sure we can write logs etc
RUN mkdir -p /app/logs && chmod -R 777 /app

# Shell form CMD so $PORT expands at container runtime.
# Single worker keeps the in-memory context store consistent — multi-worker
# uvicorn would shard contexts across processes (would need Redis to fix).
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1
