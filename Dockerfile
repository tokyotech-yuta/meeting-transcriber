FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

# soundfile / sounddevice が必要とする OS ライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存だけ先にインストールしてレイヤーキャッシュを効かせる
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --group webui

COPY app/ app/
COPY main.py ./

CMD ["uv", "run", "--no-sync", "python", "main.py"]
