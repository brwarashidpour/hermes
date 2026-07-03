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
import requests

from config import BOT_TOKEN, MAX_HISTORY_MESSAGES, SYSTEM_PROMPT
from storage import load_all, save_all
from ai_client import get_ai_response

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_RUNTIME_SECONDS = 345 * 60   # ۵ ساعت و ۴۵ دقیقه
POLL_TIMEOUT = 25
TELEGRAM_MAX_LEN = 4000


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
        providers.append({"name": name, "base_url": base_url, "model": model, "api_key": api_key})
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

    data = load_all()
    history = data.get(chat_id, [])
    history.append({"role": "user", "content": user_text})

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        extra_providers = data.get("_providers", [])
        reply, provider = get_ai_response(messages, extra_providers=extra_providers)
    except RuntimeError as e:
        print(f"همه‌ی providerها شکست خوردن: {e}")
        send_message(chat_id, "متاسفانه الان هیچ سرویس هوش مصنوعی در دسترس نیست، یکم دیگه امتحان کن.")
        return

    history.append({"role": "assistant", "content": reply})
    data[chat_id] = history[-MAX_HISTORY_MESSAGES:]
    meta = data.get("_meta", {})
    meta[chat_id] = provider
    data["_meta"] = meta
    save_all(data)
    send_message(chat_id, reply)


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
