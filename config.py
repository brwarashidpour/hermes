"""
config.py
---------
همه‌ی مقادیر حساس از environment variables خونده می‌شن — هیچ‌وقت مستقیم
تو کد ننویسشون. این ۵ تا رو باید به عنوان Secret تو GitHub Actions اضافه کنی:
BOT_TOKEN, GIST_TOKEN, GIST_ID, GEMINI_API_KEY, GROQ_API_KEY
"""
import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = os.environ["GIST_ID"]

MAX_HISTORY_MESSAGES = 20  # چند پیام آخر هر کاربر تو حافظه نگه داشته بشه

SYSTEM_PROMPT = (
    "تو یک دستیار هوش مصنوعی عمومی و کمک‌کننده‌ای. "
    "به زبانی که کاربر باهاش می‌نویسه جواب بده. خلاصه و مفید باش."
)
