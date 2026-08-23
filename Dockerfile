FROM python:3.12-slim

# Node + Claude Code CLI (the agent-sdk shells out to it under the hood).
# Git is needed by some Claude tools; ripgrep is what its Grep tool uses.
# ffmpeg + espeak-ng back the voice features (espeak-ng = local TTS).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git ripgrep ffmpeg espeak-ng \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY pyproject.toml ./
COPY app/ ./app/

# The `voice` extra is installed so both engines work out of the box. Only the
# code ships — Whisper model weights are NOT baked in; the first local
# transcription downloads them into HF_HOME below (on the /data volume, so they
# survive a container rebuild).
RUN pip install --no-cache-dir '.[voice]'

ENV DATA_DIR=/data \
    HF_HOME=/data/hf-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
