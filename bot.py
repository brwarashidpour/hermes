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

    data = load_all()
    history = data.get(chat_id, [])
    history.append({"role": "user", "content": user_text})

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        reply, provider = get_ai_response(messages)
    except RuntimeError as e:
        print(f"همه‌ی providerها شکست خوردن: {e}")
        send_message(chat_id, "متاسفانه الان هیچ سرویس هوش مصنوعی در دسترس نیست، یکم دیگه امتحان کن.")
        return

    history.append({"role": "assistant", "content": reply})
    data[chat_id] = history[-MAX_HISTORY_MESSAGES:]
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
