FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Instalar Codex CLI
RUN npm install -g @openai/codex@latest

WORKDIR /app

COPY . /app

RUN chmod +x /app/chatgpt-imagegen

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install \
        --no-cache-dir \
        -r /app/requirements-api.txt

ENV PATH="/opt/venv/bin:$PATH"
ENV HOME="/root"
ENV CHATGPT_IMAGEGEN_BACKEND="codex"
ENV CHATGPT_IMAGEGEN_NO_UPDATE_CHECK="1"

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
