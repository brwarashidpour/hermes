"""
bot.py
------
هسته‌ی اصلی: با long polling پیام‌های تلگرام رو می‌گیره، تاریخچه رو از Gist
می‌خونه، از ai_client.py جواب می‌گیره (با fallback خودکار بین providerها)،
و جواب رو برمی‌گردونه.

این اسکریپت حداکثر ~۵ ساعت و ۴۵ دقیقه اجرا می‌مونه و بعد خودش تموم می‌شه؛
کرون تو GitHub Actions هر ۶ ساعت یه اجرای تازه شروع می‌کنه.
"""
import time
import re
import requests

from config import BOT_TOKEN, MAX_HISTORY_MESSAGES, SYSTEM_PROMPT
from storage import load_all, save_all
from ai_client import get_ai_response, AllProvidersFailed

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_RUNTIME_SECONDS = 345 * 60
POLL_TIMEOUT = 25
TELEGRAM_MAX_LEN = 4000

KNOWN_KEY_PATTERNS = [
    {"prefix": "sk-ai-v1-", "name": "zenmux", "base_url": "https://zenmux.ai/api/v1", "model": "z-ai/glm-5.2-free", "tags": ["general"]},
    {"prefix": "sk-ss-v1-", "name": "zenmux", "base_url": "https://zenmux.ai/api/v1", "model": "z-ai/glm-5.2-free", "tags": ["general"]},
    {"prefix": "gsk_", "name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", "tags": ["general", "fast"]},
    {"prefix": "AIza", "name": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash", "tags": ["general"]},
    {"prefix": "sk-or-v1-", "name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free", "tags": ["general"]},
]


def classify_topic(text):
    lowered = text.lower()
    if "```" in text or any(k in lowered for k in
            ["def ", "function", "traceback", "import ", "کد ", "برنامه‌نویسی", "پایتون", "javascript", "python"]):
        return "code"
    if any(k in text for k in ["شعر", "داستان", "متن آهنگ"]):
        return "creative"
    if any(k in text for k in ["ترجمه", "translate"]):
        return "translate"
    return "general"


def parse_provider_from_code(text):
    url_match = re.search(r'(?:base_url|baseURL|api_base)["\']?\s*[=:]\s*["\']([^"\']+)["\']', text)
    key_match = re.search(r'(?:api_key|apiKey|Authorization)["\']?\s*[=:]\s*["\']?(?:Bearer\s+)?([A-Za-z0-9\-_./]{15,})["\']?', text)
    model_match = re.search(r'\bmodel["\']?\s*[=:]\s*["\']([^"\']+)["\']', text)

    if not url_match or not key_match:
        return None

    key = key_match.group(1)
    if any(bad in key.upper() for bad in ["YOUR", "XXXX", "API_KEY", "TOKEN_HERE", "HERE"]):
        return None

    base_url = url_match.group(1)
    model = model_match.group(1) if model_match else None

    domain_match = re.search(r'https?://(?:api\.)?([a-zA-Z0-9\-]+)\.', base_url)
    name = domain_match.group(1) if domain_match else "custom"

    return {"name": name, "base_url": base_url, "model": model, "api_key": key, "tags": ["general"]}


def send_message(chat_id, text):
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        chunk = text[i:i + TELEGRAM_MAX_LEN]
        try:
            requests.post(f"{API_URL}/sendMessage",
                           json={"chat_id": chat_id, "text": chunk}, timeout=15)
        except Exception as e:
            print(f"خطا در ارسال پیام: {e}")


def handle_update(update):
    message = update.get("message")
    if not message or "text" not in message:
        return
    chat_id = str(message["chat"]["id"])
    user_text = message["text"].strip()

    if user_text == "/start":
        send_message(chat_id, "سلام! هرچی بخوای می‌تونی ازم بپرسی.")
        return

    if user_text == "/reset":
        data = load_all()
        data[chat_id] = []
        save_all(data)
        send_message(chat_id, "تاریخچه پاک شد، از اول شروع کن.")
        return

    if user_text == "/help":
        send_message(chat_id,
            "دستورات:\n"
            "/start - شروع مکالمه\n"
            "/reset - پاک کردن تاریخچه و شروع دوباره\n"
            "/status - آخرین سرویس هوش مصنوعی که جواب داده\n"
            "/addprovider name url model key - اضافه کردن سرویس AI جدید\n"
            "/tag name tag1,tag2 - تعیین نقاط قوت یه سرویس (code, creative, translate, fast)\n"
            "/providers - لیست سرویس‌های اضافه‌شده\n"
            "/removeprovider name - حذف یه سرویس\n"
            "/help - همین راهنما")
        return

    if user_text == "/status":
        data = load_all()
        provider = data.get("_meta", {}).get(chat_id)
        if provider:
            send_message(chat_id, f"آخرین جواب از: {provider}")
        else:
            send_message(chat_id, "هنوز پیامی رد و بدل نشده.")
        return

    if user_text.startswith("/addprovider"):
        parts = user_text.split()[1:]
        if len(parts) != 4:
            send_message(chat_id,
                "فرمت درست (۴ تا با فاصله):\n"
                "/addprovider name base_url model api_key\n\n"
                "مثال:\n"
                "/addprovider zenmux https://api.zenmux.ai/v1 gpt-4o-mini sk-abc123")
            return
        name, base_url, model, api_key = parts
        data = load_all()
        providers = [p for p in data.get("_providers", []) if p["name"] != name]
        providers.append({"name": name, "base_url": base_url, "model": model, "api_key": api_key, "tags": ["general"]})
        data["_providers"] = providers
        save_all(data)
        send_message(chat_id, f"provider «{name}» اضافه شد و از پیام بعدی امتحان می‌شه.")
        return

    if user_text == "/providers":
        data = load_all()
        names = [p["name"] for p in data.get("_providers", [])]
        text = "providerهای اضافهشده: " + ("، ".join(names) if names else "هیچی")
        text += "\n(به
