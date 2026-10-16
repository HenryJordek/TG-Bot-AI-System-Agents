# AI Telegram Bot

A Telegram bot with local AI (Ollama) that chats with users, searches the web, and remembers personal facts.

## Features

- Local LLM via [Ollama](https://ollama.com) — no cloud, no API keys, fully offline
- Web search via DDGS when the AI needs fresh data
- Long-term memory via ChromaDB (vector database)
- Proxy and custom API support (HTTP / SOCKS5 / Cloudflare Workers)

## Commands

- `/start` — welcome message
- `/help` — show available commands

The bot also replies to any non-command message, so you can just chat with it.

## How It Works

1. User sends a message to the bot.
2. The bot runs three things in parallel:
   - decides whether a web search is needed
   - extracts any personal facts from the message
   - fetches relevant facts from memory (ChromaDB)
3. If a personal fact is found, web search and memory are skipped (the user is just talking about themselves).
4. Otherwise the bot searches the web and adds the results to the prompt.
5. The AI generates a reply using: system prompt + current time + memory + web context.
6. The reply is sent back. If it was a personal fact, it gets saved to memory.

## Installation

### Prerequisites

- Python 3.10 or higher
- [Ollama](https://ollama.com) installed and running
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Proxy or custom API (required only in regions where Telegram is blocked)

### 1. Clone the repository

    git clone https://github.com/HenryJordek/telegram-AI-Agent-bot.git
    cd telegram-AI-Agent-bot

### 2. Install Python dependencies

    pip install -r requirements.txt

### 3. Pull Ollama models

    ollama pull qwen2.5:3b
    ollama pull nomic-embed-text

`qwen2.5:3b` is the chat model (changeable in `.env`).
`nomic-embed-text` is the embedding model for ChromaDB memory.

### 4. Configure environment

Copy the example and edit it:

    cp .env.example .env

Fill in `.env`:

    # Telegram Bot Configuration
    TOKEN=your_telegram_bot_token_here
    PROXY=http://127.0.0.1:10808
    API=

    LOG_LEVEL=INFO
    DEBUG=0

    MODEL_NAME=qwen2.5:3b
    CHROMADB_PATH=./chromadb
    EMBED_MODEL=nomic-embed-text
    OLLAMA_URL=http://localhost:11434
    MAIN_DEVICE=gpu
    AI_PROMPT=You are a helpful AI assistant. Reply concisely.

### 5. Get a Bot Token

- Open Telegram and message [@BotFather](https://t.me/BotFather)
- Send `/newbot` and follow instructions
- Copy the token into `TOKEN=` in `.env`

### 6. Configure Proxy or API (optional)

If Telegram is blocked in your region, set one of:

    HTTP proxy:    PROXY=http://127.0.0.1:8080
    SOCKS5 proxy:  PROXY=socks5://127.0.0.1:1080
    Custom API:    API=https://your-cloudflare-worker.example.com

Only one of `PROXY` / `API` is needed. If both are set, `PROXY` wins.

### 7. Run the bot

    start.bat

Or directly:

    python main.py

## Project Structure

    main.py              # Main bot application
    .env                 # Environment variables (not in git)
    .env.example         # Example environment file
    .gitignore           # Git ignore rules
    readme.md            # This file (English)
    readme-ru.md         # Same file in Russian
    requirements.txt     # Python dependencies
    start.bat            # Windows launcher

## Data Storage

Chat history is **not** stored. Only personal facts about users (name, age, job, preferences) are saved to ChromaDB as vector embeddings, and only for the current user.

Data lives in the folder specified by `CHROMADB_PATH`.

## Troubleshooting

- **"Model not found"** — set `MODEL_NAME` in `.env` and run `ollama pull <model>`.
- **Bot doesn't reply** — check `PROXY` / `API` in `.env`, and that `ollama serve` is running.
- **First reply is slow** — the model is loading into VRAM. The bot warms it up on startup, but if you skip warmup the first request can take 30+ seconds.
- **Model stays in VRAM after exit** — this is normal (Ollama keeps it for 24h). To unload manually: `ollama stop <model>`.

## License

MIT License — free for personal and commercial use.

## Contributing

Feel free to submit issues and pull requests.