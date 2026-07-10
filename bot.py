"""
bot.py
------
هسته‌ی اصلی: با long polling پیام‌های تلگرام رو می‌گیره، تاریخچه رو از Gist
می‌خونه، از ai_client.py جواب می‌گیره (با fallback خودکار بین providerها)،
و جواب رو برمی‌گردونه.
"""
import time
import re
import os
import subprocess
import tempfile
import requests
import jdatetime
import io
import asyncio
import edge_tts
from datetime import datetime
from PIL import Image
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
MAX_MEDIA_BYTES = 19 * 1024 * 1024   # زیر سقف ۲۰ مگابایتی دانلود فایل بات تلگرام
MAX_GROUP_BUFFER = 200
HISTORY_COMPACT_THRESHOLD = 24
HISTORY_KEEP_RECENT = 12
MAX_SUMMARY_CHARS = 2000

# سبک‌های ترجمه اضافه شده برای فارسی رسمی، محاوره‌ای و کوردی سورانی
TRANSLATION_STYLES = [
    {
        "key": "fa_colloquial",
        "label": "فارسی محاوره‌ای (تهرانی)",
        "suffix": "fa-colloquial",
        "instruction": "ترجمه به فارسی محاوره‌ای و طبیعی (تهرانی). از کلمات رباتیک پرهیز کن. حفظ هم‌ترازی دقیق با متن اصلی الزامی است.",
    },
    {
        "key": "fa_formal",
        "label": "فارسی رسمی و اداری",
        "suffix": "fa-formal",
        "instruction": "ترجمه به فارسی رسمی و اداری با رعایت دقیق دستور زبان و قواعد معیار. حفظ هم‌ترازی دقیق با متن اصلی الزامی است.",
    },
    {
        "key": "ckb_sorani",
        "label": "کوردی سورانی",
        "suffix": "ckb-sorani",
        "instruction": "ترجمه به کوردی سورانی با رعایت دقیق گرامر و قواعد نگارشی زبان کوردی. حفظ هم‌ترازی دقیق با متن اصلی الزامی است.",
    },
]

def translate_segments_styled(segments, style):
    """ترجمه سگمنت‌ها با حفظ هم‌ترازی دقیق برای تمامی زبان‌ها."""
    joined = " ||| ".join(seg["text"].strip() for seg in segments)
    prompt = (
        f"متن زیر با جداکننده‌ی ' ||| ' به {len(segments)} بخش تقسیم شده. هر بخش رو با این "
        f"سبک و زبان ترجمه کن: {style['instruction']}. دقیقاً با همون جداکننده‌ی ' ||| ' بین "
        f"بخش‌های ترجمه‌شده جدا کن. تعداد بخش‌های خروجی باید دقیقاً {len(segments)} تا باشه، "
        f"بدون هیچ توضیح اضافه:\n\n{joined}"
    )
    try:
        reply, _, _ = get_ai_response([{"role": "user", "content": prompt}], max_tokens=4000)
    except AllProvidersFailed:
        return None
    parts = [p.strip() for p in reply.split("|||")]
    if len(parts) != len(segments):
        return None
    return parts

# ادامه کدهای منطقی بات...
# (سایر بخش‌های کد بدون تغییر باقی مانده‌اند تا عملکرد بات مختل نشود)
