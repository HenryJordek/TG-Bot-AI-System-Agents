import os
import re
import json
import uuid
import socket
import asyncio
import logging
import aiohttp
import datetime
import requests
import chromadb
from aiogram import F
from ddgs import DDGS
from dotenv import load_dotenv
from aiogram.types import Message
from aiogram.filters import Command
from aiohttp_socks import ProxyConnector
from aiogram.methods import DeleteWebhook
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

load_dotenv()

def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on", "д", "да")

DEBUG     = _env_bool("DEBUG", False)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

if DEBUG:
    logging.getLogger().setLevel(logging.DEBUG)

for noisy in (
    "aiohttp", "httpcore", "httpx", "urllib3",
    "chromadb", "posthog",
    "ddgs", "primp", "rustls", "hickory_dns",
    "asyncio",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)

MODEL_NAME    = os.getenv("MODEL_NAME")
CHROMADB_PATH = os.getenv("CHROMADB_PATH")
TOKEN         = os.getenv("TOKEN")
PROXY         = os.getenv("PROXY")
API           = os.getenv("API")
BOT_NAME      = os.getenv("BOT_NAME", "")
ADMIN_ID_RAW  = os.getenv("ADMIN_ID", "")
ADMIN_ID      = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.strip().isdigit() else None
MAIN_DEVICE   = os.getenv("MAIN_DEVICE", "gpu")
OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL   = os.getenv("EMBED_MODEL", "nomic-embed-text")
AI_PROMPT     = os.getenv("AI_PROMPT", "")

if not MODEL_NAME:
    raise ValueError("Model not found! Please indicate MODEL_NAME in .env file")
if not CHROMADB_PATH:
    raise ValueError("Chromadb_path not found! Please indicate CHROMADB_PATH in .env file")
if not TOKEN:
    raise ValueError("Token not found! Please indicate TOKEN in .env file")
if not PROXY and not API:
    log.warning("Proxy or API not found! Connecting to Telegram directly")

CHAT_URL   = f"{OLLAMA_URL}/api/chat"
KEEP_ALIVE = "24h"

bot: Bot | None = None
dp = Dispatcher(storage=MemoryStorage())

client = chromadb.PersistentClient(path=CHROMADB_PATH)
embedding_fn = OllamaEmbeddingFunction(url=OLLAMA_URL, model_name=EMBED_MODEL)
collection = client.get_or_create_collection(name="AI_memory", embedding_function=embedding_fn)

_session: aiohttp.ClientSession | None = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def ollama_chat(
    system_prompt: str,
    user_text: str,
    *,
    json_format: bool = False,
    timeout: int = 180,
) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_text},
        ],
        "stream": False,
        "options": {"num_gpu": 99 if MAIN_DEVICE == "gpu" else 0},
        "keep_alive": KEEP_ALIVE,
    }
    if json_format:
        payload["format"] = "json"

    session = await get_session()
    try:
        async with session.post(
            CHAT_URL, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("Ollama HTTP %s: %s", resp.status, body[:300])
                return ""
            data = await resp.json()
            return (data.get("message") or {}).get("content", "") or ""
    except Exception as e:
        log.exception("Ollama request failed: %s", e)
        return ""

def extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None

async def warmup_ollama() -> None:
    log.info("Warming up model %s ...", MODEL_NAME)
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "options": {"num_gpu": 99 if MAIN_DEVICE == "gpu" else 0},
        "keep_alive": KEEP_ALIVE,
    }
    session = await get_session()
    try:
        async with session.post(
            CHAT_URL, json=payload, timeout=aiohttp.ClientTimeout(total=300)
        ) as resp:
            if resp.status == 200:
                log.info("Model warmed up and ready")
            else:
                log.warning("Warmup returned HTTP %s", resp.status)
    except Exception as e:
        log.warning("Warmup failed (non-critical): %s", e)

SEARCH_PROMPT = (
    "Ты — агент поиска в интернете. Реши, нужно ли искать в интернете, чтобы ответить на сообщение.\n\n"
    "Правила:\n"
    "1. Ищи при запросах конкретных данных: новости, цены, погода, личности, факты, характеристики, расписания, события.\n"
    "2. Ищи, если есть конкретное имя/название/сущность, о которой у тебя нет точных данных.\n"
    "3. На «кто такой…» формируй запрос СТРОГО из имени (например: «Александр Пушкин»). Без «сегодня», «в интернете» и т.п.\n"
    "4. НЕ ищи: приветствия, болтовню, код, философию.\n"
    "5. search=true → queries: 1–2 коротких фразы. search=false → queries: [].\n"
    "6. НЕ ищи, если пользователь сообщает информацию о СЕБЕ: своё имя, возраст, профессию, "
    "предпочтения, или просит «запомни …». Это личный факт, а не запрос в интернет.\n"
    "7. Ищи имя/название ТОЛЬКО если явно спрашивают «кто такой X», «что такое X», «расскажи про X».\n\n"
    "Формат ответа строго JSON: {\"search\": true/false, \"queries\": [\"...\"]}\n\n"
    "Примеры:\n"
    "'Расскажи про Александра Пушкина.' -> {\"search\": true, \"queries\": [\"Александр Пушкин\"]}\n"
    "'Сколько стоит iPhone 15?' -> {\"search\": true, \"queries\": [\"iPhone 15 цена\"]}\n"
    "'Что такое квантовый компьютер?' -> {\"search\": true, \"queries\": [\"квантовый компьютер\"]}\n"
    "'Привет, как дела?' -> {\"search\": false, \"queries\": []}\n"
    "'Напиши функцию на Python.' -> {\"search\": false, \"queries\": []}\n"
    "'Меня зовут Henry, запомни.' -> {\"search\": false, \"queries\": []}\n"
    "'Я работаю программистом.' -> {\"search\": false, \"queries\": []}"
)

async def analyze_query(text: str) -> tuple[bool, list[str]]:
    resp = await ollama_chat(SEARCH_PROMPT, text, json_format=True, timeout=60)
    data = extract_json(resp)
    log.debug("[Search] Query: %s -> Response: %s", text, resp)
    if not isinstance(data, dict):
        return False, []
    queries = data.get("queries")
    if not isinstance(queries, list):
        queries = []
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if data.get("search"):
        return True, (queries[:2] or [text])
    return False, []

def _ddgs_search(query: str, max_results: int = 2) -> list[dict]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))

async def search_web(queries: list[str]) -> str:
    if not queries:
        return ""
    loop = asyncio.get_running_loop()
    snippets: list[str] = []
    for q in queries:
        try:
            results = await loop.run_in_executor(None, _ddgs_search, q)
            log.debug("[Search] Query: %s -> Found: %d results", q, len(results))
            if results:
                body = "\n".join(
                    f"{r.get('title','')}: {r.get('body','')}"[:500] for r in results
                )
                snippets.append(f"[Запрос: {q}]\n{body}")
        except Exception as e:
            log.warning("[Search] '%s' error: %s", q, e)
            snippets.append(f"[Запрос: {q}] Ошибка: {e}")
    result = "\n---\n".join(snippets)
    if len(result) < 100:
        return ""
    return result

FACT_PROMPT = (
    "Ты — фильтр памяти. Извлеки из сообщения пользователя краткий факт о нём "
    "(имя, возраст, профессия, предпочтения).\n"
    "Если в сообщении НЕТ факта о пользователе — верни {\"fact\": \"NONE\"}.\n"
    "Никогда не пиши NONE вместе с префиксом типа «Имя:». Просто NONE.\n\n"
    "Формат ответа строго JSON: {\"fact\": \"...\"}\n\n"
    "Примеры:\n"
    "'Привет, как дела?' -> {\"fact\": \"NONE\"}\n"
    "'Меня зовут Иван' -> {\"fact\": \"Имя: Иван\"}\n"
    "'Я программист на питоне' -> {\"fact\": \"Профессия: программист на питоне\"}\n"
    "'Люблю играть в доту' -> {\"fact\": \"Предпочтение: любит играть в доту\"}\n"
    "'А имя моё помнишь?' -> {\"fact\": \"NONE\"}\n"
    "'Запоминай Henry' -> {\"fact\": \"Имя: Henry\"}"
)

async def extract_user_fact(text: str) -> str:
    resp = await ollama_chat(FACT_PROMPT, text, json_format=True, timeout=60)
    data = extract_json(resp)
    fact = ""
    if isinstance(data, dict):
        fact = str(data.get("fact", "")).strip()
    if not fact:
        fact = (resp or "").strip().strip('"').strip("'")
    log.debug("[Memory] Query: %s -> Response: %s", text, fact)

    if not fact or len(fact) <= 2:
        return ""
    if "NONE" in fact.upper():
        return ""
    return fact

async def save_memory(user_id: int, fact: str) -> None:
    if not fact:
        return
    loop = asyncio.get_running_loop()
    fact_id = f"{user_id}_{uuid.uuid4().hex[:8]}"
    try:
        await loop.run_in_executor(
            None,
            lambda: collection.add(
                documents=[fact],
                metadatas=[{"user_id": str(user_id)}],
                ids=[fact_id],
            ),
        )
        log.debug("Saved fact: %s | ID: %s", fact, fact_id)
    except Exception as e:
        log.error("Failed to save memory: %s", e)

async def get_memory(user_id: int, query: str, max_dist: float = 0.8) -> str:
    loop = asyncio.get_running_loop()

    def _query():
        return collection.query(
            query_texts=[query],
            n_results=3,
            where={"user_id": str(user_id)},
        )

    try:
        results = await loop.run_in_executor(None, _query)
    except Exception as e:
        log.error("get_memory for user_id=%s: %s", user_id, e)
        return ""

    docs  = (results.get("documents") or [[]])[0]
    dists = (results.get("distances") or [[]])[0]
    valid = [d for d, s in zip(docs, dists) if s is not None and s < max_dist]
    if not valid:
        return ""
    log.debug("[Memory] facts:\n%s", "\n".join(f"- {f}" for f in valid))
    return "\n".join(f"- {f}" for f in valid)

async def get_username(user_id: int) -> str | None:
    try:
        chat: types.Chat = await bot.get_chat(chat_id=user_id)
        return chat.username
    except Exception as e:
        log.warning("not found username for ID %s: %s", user_id, e)
        return None

def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts

@dp.message(Command("start"))
async def cmd_start(message: Message):
    name = f" Я {BOT_NAME}." if BOT_NAME else ""
    await message.answer(f"Привет!{name} Спроси меня о чём-нибудь!")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("/help — справка\n/start — приветствие\n")

@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def chat(message: Message):
    user_message = message.text
    user_id = message.from_user.id

    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass

    fact, (should_search, queries), memory_context = await asyncio.gather(
        extract_user_fact(user_message),
        analyze_query(user_message),
        get_memory(user_id, user_message),
    )

    if fact:
        should_search, queries = False, []
        memory_context = ""
        log.debug("[Logic] Personal fact detected -> search and memory disabled")

    web_context = await search_web(queries) if should_search else ""
    log.debug("[AI] Web context: %s", "YES" if web_context else "NO")

    now = datetime.datetime.now()
    weekdays = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    time_context = f"[Текущее время]: {now.strftime('%d.%m.%Y %H:%M')}, {weekdays[now.weekday()]}."

    system_prompt = f"{AI_PROMPT}\n\n{time_context}".strip()

    if memory_context:
        system_prompt += (
            f"\n\n[Информация о пользователе]:\n{memory_context}\n\n"
            "ПРАВИЛО: Используй эту информацию ТОЛЬКО если пользователь явно спрашивает о себе "
            "(имя, возраст, профессия). Если пользователь просит код, техническую помощь или новости — "
            "ПОЛНОСТЬЮ ИГНОРИРУЙ этот блок. Никогда не упоминай имя пользователя без прямой просьбы."
        )
    else:
        system_prompt += (
            "\n\n[Информация о пользователе]: отсутствует. "
            "Ты НЕ знаешь имени пользователя и ничего о нём. "
            "Если он спрашивает «помнишь ли ты меня», «как меня зовут» — честно скажи, что не помнишь."
        )

    if web_context:
        system_prompt += f"\n\n[Данные из интернета]:\n{web_context}"

    reply = await ollama_chat(system_prompt, user_message, timeout=180)
    if not reply:
        await message.answer("Не удалось получить ответ от модели. Попробуйте позже.")
        return

    log.debug("Response to %s: %s", user_id, reply)

    for part in split_message(reply):
        await message.answer(part)

    if fact:
        await save_memory(user_id, fact)

async def main():
    global bot
    if PROXY:
        session = AiohttpSession(proxy=PROXY)
        log.info("proxy: %s", PROXY)
    elif API:
        server = TelegramAPIServer.from_base(API)
        session = AiohttpSession(api=server)
        log.info("api: %s", API)
    else:
        session = AiohttpSession()
        log.info("direct connection (no proxy, no custom API)")

    bot = Bot(token=TOKEN, session=session)
    log.info("Bot starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Webhook deleted")

    await warmup_ollama()

    log.info("Bot started (debug=%s)", DEBUG)
    try:
        await dp.start_polling(bot)
    finally:
        if _session and not _session.closed:
            await _session.close()

if __name__ == "__main__":
    asyncio.run(main())