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
from bs4 import BeautifulSoup

from config import BOT_TOKEN, MAX_HISTORY_MESSAGES, SYSTEM_PROMPT
from storage import load_all, save_all
from ai_client import get_ai_response, AllProvidersFailed, ask_single_provider, PROVIDERS as BUILTIN_PROVIDERS

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_RUNTIME_SECONDS = 345 * 60   # ۵ ساعت و ۴۵ دقیقه
POLL_TIMEOUT = 25
TELEGRAM_MAX_LEN = 4000
MAX_KNOWLEDGE_CHARS = 6000
MAX_LEARNED_URLS = 3
MAX_CHARS_PER_URL = 3000


def fetch_url_text(url):
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text("\n", strip=True)[:MAX_CHARS_PER_URL]

# اگه کاربر فقط خود کلید رو بفرسته (بدون دستور کامل)، از روی فرمتش
# تشخیص می‌دیم مال کدوم سرویسه.
KNOWN_KEY_PATTERNS = [
    {"prefix": "sk-ai-v1-", "name": "zenmux", "base_url": "https://zenmux.ai/api/v1", "model": "z-ai/glm-5.2-free", "tags": ["general"]},
    {"prefix": "sk-ss-v1-", "name": "zenmux", "base_url": "https://zenmux.ai/api/v1", "model": "z-ai/glm-5.2-free", "tags": ["general"]},
    {"prefix": "gsk_", "name": "groq", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", "tags": ["general", "fast"]},
    {"prefix": "AIza", "name": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash", "tags": ["general"]},
    {"prefix": "sk-or-v1-", "name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "openrouter/free", "tags": ["general"]},
    {"prefix": "csk-", "name": "cerebras", "base_url": "https://api.cerebras.ai/v1", "model": "llama-3.3-70b", "tags": ["general", "fast"]},
]


def classify_topic(text):
    """یه تشخیص ساده و کلمه‌کلیدی‌ه، نه درک واقعی معنایی — فقط برای اولویت‌بندی provider."""
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
    """از یه تکه کد نمونه (پایتون/جاوااسکریپت/curl) که سایت‌ها می‌دن، سعی می‌کنه
    base_url، model و api_key رو خودکار دربیاره."""
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
            "/goal متن - ثبت هدف بلندمدت که تو همه‌ی جواب‌ها رعایت بشه\n"
            "/learn متن‌یا‌لینک - یادگیری یه سند یا سایت برای استفاده تو جواب‌های بعدی\n"
            "/forget - پاک کردن دانشی که با /learn دادی\n"
            "/moa سوال - گرفتن نظر همه‌ی سرویس‌ها و ترکیب بهترین جواب\n"
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
        text = "providerهای اضافه‌شده: " + ("، ".join(names) if names else "هیچی")
        text += "\n(به‌علاوه‌ی gemini و groq که تو کد ثابتن)"
        send_message(chat_id, text)
        return

    if user_text.startswith("/tag "):
        parts = user_text.split()[1:]
        if len(parts) != 2:
            send_message(chat_id, "فرمت درست:\n/tag name tag1,tag2\n\nمثال:\n/tag zenmux code,creative")
            return
        name, tags_str = parts
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        data = load_all()
        providers = data.get("_providers", [])
        found = False
        for p in providers:
            if p["name"] == name:
                p["tags"] = tags
                found = True
        if not found:
            send_message(chat_id, f"provider «{name}» پیدا نشد (فقط providerهایی که خودت اضافه کردی قابل تگ‌زدنن).")
            return
        data["_providers"] = providers
        save_all(data)
        send_message(chat_id, f"تگ‌های «{name}» شد: {', '.join(tags)}")
        return

    if user_text.startswith("/removeprovider"):
        parts = user_text.split()[1:]
        if len(parts) != 1:
            send_message(chat_id, "فرمت درست:\n/removeprovider name")
            return
        name = parts[0]
        data = load_all()
        providers = [p for p in data.get("_providers", []) if p["name"] != name]
        data["_providers"] = providers
        save_all(data)
        send_message(chat_id, f"provider «{name}» حذف شد (اگه بود).")
        return

    if user_text.startswith("/goal"):
        goal_text = user_text[len("/goal"):].strip()
        data = load_all()
        if not goal_text:
            goal = data.get("_goal")
            send_message(chat_id, f"هدف فعلی: {goal}" if goal else "هنوز هدفی ثبت نشده. مثال:\n/goal هر روز ۳۰ دقیقه انگلیسی تمرین کنم")
            return
        data["_goal"] = goal_text
        save_all(data)
        send_message(chat_id, f"هدف بلندمدت ثبت شد: {goal_text}")
        return

    if user_text.startswith("/learn "):
        arg = user_text[len("/learn "):].strip()
        if arg.startswith("http://") or arg.startswith("https://"):
            data = load_all()
            urls = data.get("_learned_urls", [])
            if arg not in urls:
                urls.append(arg)
            data["_learned_urls"] = urls[-MAX_LEARNED_URLS:]
            save_all(data)
            send_message(chat_id,
                f"ثبت شد. از این به بعد قبل از هر جواب، لحظه‌ای دوباره از این لینک می‌خونم "
                f"(یعنی همیشه آخرین نسخه‌ست، نه یه اسنپ‌شات قدیمی) — فقط هر جواب چند ثانیه کندتر می‌شه.\n"
                f"لینک‌های ثبت‌شده: {len(data['_learned_urls'])}")
            return

        content = arg[:MAX_KNOWLEDGE_CHARS]
        data = load_all()
        knowledge = data.get("_knowledge", "")
        knowledge = (knowledge + "\n\n---\n\n" + content) if knowledge else content
        knowledge = knowledge[-MAX_KNOWLEDGE_CHARS:]
        data["_knowledge"] = knowledge
        save_all(data)
        send_message(chat_id,
            f"ذخیره شد ({len(content)} کاراکتر). از سوال بعدی به‌عنوان دانش زمینه استفاده می‌شه.\n"
            f"⚠️ این واقعی train شدن نیست؛ فقط این متن رو هر بار به کانتکست سوالت اضافه می‌کنم.")
        return

    if user_text == "/forget":
        data = load_all()
        data["_knowledge"] = ""
        data["_learned_urls"] = []
        save_all(data)
        send_message(chat_id, "دانش ذخیره‌شده و لینک‌های یادگرفته‌شده پاک شدن.")
        return

    if user_text.startswith("/moa "):
        question = user_text[len("/moa "):].strip()
        data = load_all()
        all_providers = data.get("_providers", []) + BUILTIN_PROVIDERS
        if len(all_providers) < 2:
            send_message(chat_id, "برای moa حداقل ۲ تا سرویس فعال لازمه.")
            return

        send_message(chat_id, f"در حال گرفتن نظر از {len(all_providers)} سرویس... کمی طول می‌کشه.")
        opinions = []
        for p in all_providers:
            try:
                answer = ask_single_provider(p, [{"role": "user", "content": question}])
                opinions.append(f"[{p['name']}]:\n{answer}")
            except Exception as e:
                print(f"moa: {p['name']} شکست خورد: {e}")
                continue

        if not opinions:
            send_message(chat_id, "هیچ سرویسی جواب نداد.")
            return

        synthesis_prompt = (
            "چند نظر مختلف از مدل‌های مختلف هوش مصنوعی به یه سوال زیرن. "
            "با ترکیب نکات درست و خوب هرکدوم، بهترین و کامل‌ترین جواب رو بساز:\n\n"
            + "\n\n".join(opinions) + f"\n\nسوال اصلی: {question}"
        )
        try:
            final_answer, used, _ = get_ai_response([{"role": "user", "content": synthesis_prompt}])
            send_message(chat_id, final_answer)
        except AllProvidersFailed:
            send_message(chat_id, "نظرها جمع شد ولی نتونستم ترکیبشون کنم:\n\n" + "\n\n".join(opinions))
        return

    if not user_text.startswith("/") and any(kw in user_text for kw in
            ["base_url", "baseURL", "api_base", "api_key", "apiKey"]):
        parsed = parse_provider_from_code(user_text)
        if parsed and parsed["model"]:
            data = load_all()
            providers = [p for p in data.get("_providers", []) if p["name"] != parsed["name"]]
            providers.append(parsed)
            data["_providers"] = providers
            save_all(data)
            send_message(chat_id,
                f"از کد تشخیص دادم:\nname: {parsed['name']}\nurl: {parsed['base_url']}\n"
                f"model: {parsed['model']}\nاضافه شد و از پیام بعدی امتحان می‌شه.")
            return
        if parsed and not parsed["model"]:
            send_message(chat_id,
                f"url و کلید رو پیدا کردم ولی اسم مدل مشخص نبود. این رو بفرست:\n"
                f"/addprovider {parsed['name']} {parsed['base_url']} MODEL_NAME {parsed['api_key']}")
            return
        send_message(chat_id,
            "نتونستم از این متن url و کلید رو دربیارم. یا کامل با /addprovider بفرست، "
            "یا اگه سرویس شناخته‌شده‌ست فقط خود کلید رو تنها بفرست.")
        return

    if " " not in user_text and not user_text.startswith("/") and len(user_text) > 15:
        for pattern in KNOWN_KEY_PATTERNS:
            if user_text.startswith(pattern["prefix"]):
                data = load_all()
                providers = [p for p in data.get("_providers", []) if p["name"] != pattern["name"]]
                providers.append({
                    "name": pattern["name"],
                    "base_url": pattern["base_url"],
                    "model": pattern["model"],
                    "api_key": user_text,
                })
                data["_providers"] = providers
                save_all(data)
                send_message(chat_id, f"از فرمتش تشخیص دادم مال «{pattern['name']}»ه، اضافه شد و از پیام بعدی امتحان می‌شه.")
                return

    data = load_all()
    history = data.get(chat_id, [])
    history.append({"role": "user", "content": user_text})

    try:
        system_prompt = SYSTEM_PROMPT
        goal = data.get("_goal")
        if goal:
            system_prompt += f"\n\nهدف بلندمدت کاربر که باید در پاسخ‌ها بهش پایبند باشی: {goal}"
        knowledge = data.get("_knowledge")
        if knowledge:
            system_prompt += f"\n\nاطلاعات زمینه‌ای که کاربر قبلاً بهت داده (در صورت ربط به سوال ازش استفاده کن):\n{knowledge}"

        for url in data.get("_learned_urls", []):
            try:
                live_text = fetch_url_text(url)
                system_prompt += f"\n\nمحتوای زنده‌ی {url} (تازه، همین الان خونده شده):\n{live_text}"
            except Exception as e:
                print(f"خطا در خوندن لینک یادگرفته‌شده {url}: {e}")

        messages = [{"role": "system", "content": system_prompt}] + history
        extra_providers = data.get("_providers", [])
        topic = classify_topic(user_text)
        reply, provider, dead_providers = get_ai_response(messages, extra_providers=extra_providers, topic=topic)
    except AllProvidersFailed as e:
        print(f"همه‌ی providerها شکست خوردن: {e}")
        if e.dead_providers:
            data["_providers"] = [p for p in data.get("_providers", []) if p["name"] not in e.dead_providers]
            save_all(data)
        send_message(chat_id, "متاسفانه الان هیچ سرویس هوش مصنوعی در دسترس نیست، یکم دیگه امتحان کن.")
        return

    history.append({"role": "assistant", "content": reply})
    data[chat_id] = history[-MAX_HISTORY_MESSAGES:]
    meta = data.get("_meta", {})
    meta[chat_id] = provider
    data["_meta"] = meta
    removed_names = []
    if dead_providers:
        removed_names = [p["name"] for p in data.get("_providers", []) if p["name"] in dead_providers]
        data["_providers"] = [p for p in data.get("_providers", []) if p["name"] not in dead_providers]
    save_all(data)
    send_message(chat_id, reply)
    if removed_names:
        send_message(chat_id, f"⚠️ سرویس(های) {'، '.join(removed_names)} دیگه جواب نمی‌دن (کلید نامعتبر یا اعتبار تموم)، از لیست حذف شدن.")


def main():
    # هر webhook قدیمی که رو این توکن مونده باشه رو پاک می‌کنیم، وگرنه
    # getUpdates با خطای 409 Conflict برخورد می‌کنه.
    try:
        requests.get(f"{API_URL}/deleteWebhook", timeout=15)
    except Exception as e:
        print(f"خطا در deleteWebhook: {e}")

    start_time = time.time()
    offset = None
    print("بات روشن شد...")

    while time.time() - start_time < MAX_RUNTIME_SECONDS:
        params = {"timeout": POLL_TIMEOUT}
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(f"{API_URL}/getUpdates", params=params,
                                 timeout=POLL_TIMEOUT + 10)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except Exception as e:
            print(f"خطا در getUpdates: {e}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as e:
                print(f"خطا در پردازش پیام: {e}")

    print("زمان این اجرا تموم شد؛ اجرای بعدی طبق کرون شروع می‌شه.")


if __name__ == "__main__":
    main()
