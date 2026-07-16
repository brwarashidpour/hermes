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
import os
import subprocess
import tempfile
import requests
import jdatetime
import asyncio
import edge_tts
from datetime import datetime
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


def compact_history(chat_id, data):
    """اگه تاریخچه خیلی طولانی شده، بخش قدیمیش رو (به‌جای دور ریختن خام) یه‌بار
    خلاصه می‌کنه و نگه می‌داره — زمینه حفظ می‌شه ولی توکن خیلی کمتر مصرف می‌شه."""
    history = data.get(chat_id, [])
    if len(history) <= HISTORY_COMPACT_THRESHOLD:
        return history

    old_part = history[:-HISTORY_KEEP_RECENT]
    recent_part = history[-HISTORY_KEEP_RECENT:]
    old_text = "\n".join(f"{m['role']}: {m['content']}" for m in old_part)
    prompt = (
        "مکالمه‌ی زیر رو خیلی فشرده و خلاصه کن — فقط نکات و اطلاعاتی که ممکنه بعداً "
        "لازم بشه، بدون جزئیات جانبی. خروجی یه پاراگراف کوتاه:\n\n" + old_text
    )
    try:
        summary, _, _ = get_ai_response([{"role": "user", "content": prompt}], max_tokens=300)
    except AllProvidersFailed:
        return recent_part  # اگه خلاصه‌سازی نشد، حداقل قدیمی‌ها رو کات کن

    summaries = data.get("_history_summary", {})
    existing = summaries.get(chat_id, "")
    combined = (existing + "\n" + summary).strip() if existing else summary
    summaries[chat_id] = combined[-MAX_SUMMARY_CHARS:]
    data["_history_summary"] = summaries
    return recent_part
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_TRANSLATE_LANG = "fa"


def normalize_text(text):
    """فاصله و خط خالی اضافی رو حذف می‌کنه، بدون این‌که محتوا کم بشه — کاهش توکن مجانی."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_url_text(url):
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return normalize_text(soup.get_text("\n", strip=True))[:MAX_CHARS_PER_URL]


def _fmt(seconds, sep):
    total_ms = int(round(seconds * 1000))
    h = total_ms // 3600000
    m = (total_ms % 3600000) // 60000
    s = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def build_srt(segments):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_fmt(seg['start'], ',')} --> {_fmt(seg['end'], ',')}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines) + "\n"


def build_vtt(segments):
    lines = [
        "WEBVTT",
        "",
        "STYLE",
        "::cue {",
        "  font-family: Vazirmatn, 'IRANSans', 'B Nazanin', Tahoma, sans-serif;",
        "  font-size: 105%;",
        "  direction: rtl;",
        "}",
        "",
    ]
    for seg in segments:
        lines.append(f"{_fmt(seg['start'], '.')} --> {_fmt(seg['end'], '.')}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def build_sbv(segments):
    lines = []
    for seg in segments:
        lines.append(f"{_fmt(seg['start'], '.')},{_fmt(seg['end'], '.')}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def build_txt(segments):
    return "\n".join(seg["text"].strip() for seg in segments)


PERSIAN_VOICE = "fa-IR-DilaraNeural"   # زن؛ برای صدای مرد: fa-IR-FaridNeural
KURDISH_VOICE_CANDIDATES = ["ckb-IQ-SorooshNeural", "ckb-IQ-NazaninNeural"]  # تأییدشده نیست، best-effort


async def _tts_generate(text, output_path, voice):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def text_to_speech_fa(text, output_path, voice=PERSIAN_VOICE):
    asyncio.run(_tts_generate(text, output_path, voice))


def send_audio_file(chat_id, file_path, caption=""):
    try:
        with open(file_path, "rb") as f:
            requests.post(
                f"{API_URL}/sendAudio",
                data={"chat_id": chat_id, "caption": caption},
                files={"audio": (os.path.basename(file_path), f)},
                timeout=120,
            )
    except Exception as e:
        print(f"خطا در ارسال صدا: {e}")


TRANSLATION_STYLES = [
    {
        "key": "fa_colloquial",
        "label": "فارسی محاوره‌ای",
        "suffix": "fa-colloquial",
        "instruction": "فارسی محاوره‌ای و طبیعی، شبیه فارسی روزمره‌ی تهرانی، از کلمات رباتیک و ترجمه‌ی کلمه‌به‌کلمه پرهیز کن",
    },
    {
        "key": "fa_formal",
        "label": "فارسی رسمی و اداری",
        "suffix": "fa-formal",
        "instruction": "فارسی رسمی و اداری، کاملاً منطبق با گرامر و قواعد زبان معیار فارسی، مناسب مکاتبات رسمی",
    },
    {
        "key": "ckb_sorani",
        "label": "کوردی سورانی",
        "suffix": "ckb-sorani",
        "instruction": "کوردی سورانی (نه کرمانجی)، با رعایت دقیق گرامر و قواعد نگارشی زبان کوردی سورانی",
    },
]


def translate_segments_styled(segments, style):
    """مثل translate_segments ولی با یه سبک/زبان مشخص (از TRANSLATION_STYLES)."""
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


def translate_full_text_natural(full_text, instruction):
    """کل متن رو یکجا و روون (نه تیکه‌تیکه) طبق instruction ترجمه می‌کنه — مخصوص متن
    مرجع/صدا، با علامت‌گذاری درست تا موقع خونده‌شدن طبیعی به‌نظر بیاد."""
    prompt = f"{instruction}\n\nفقط متن ترجمه‌شده رو بده، بدون توضیح اضافه:\n\n{full_text}"
    try:
        translated, _, _ = get_ai_response([{"role": "user", "content": prompt}], max_tokens=4000)
        return translated
    except AllProvidersFailed:
        return None


def try_generate_speech(text, output_path, voice_candidates):
    """چند تا کد صدا رو امتحان می‌کنه؛ اولین کدوم که کار کرد استفاده می‌شه. اگه هیچ‌کدوم
    کار نکرد، None برمی‌گردونه (به‌جای کرش کردن)."""
    for voice in voice_candidates:
        try:
            text_to_speech_fa(text, output_path, voice=voice)
            return voice
        except Exception as e:
            print(f"صدای {voice} کار نکرد: {e}")
            continue
    return None


def transcribe_audio(file_path):
    """صدا رو با Groq Whisper به متن (با زمان‌بندی هر بخش) تبدیل می‌کنه."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            GROQ_WHISPER_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": f},
            data={"model": "whisper-large-v3", "response_format": "verbose_json",
                  "timestamp_granularities[]": "segment"},
            timeout=180,
        )
    resp.raise_for_status()
    data = resp.json()
    segments = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in data.get("segments", [])]
    return segments, data.get("language", "نامشخص")


TRANSLATION_TARGETS = [
    {"key": "fa_casual", "label": "فارسی محاوره‌ای", "lang_code": "fa",
     "style": "محاوره‌ای و طبیعی، مثل فارسی روزمره‌ی تهرانی، بدون کلمات رباتیک",
     "tts_voice": "fa-IR-DilaraNeural"},
    {"key": "fa_formal", "label": "فارسی رسمی", "lang_code": "fa",
     "style": "کاملاً رسمی و اداری، با دستور زبان دقیق و واژگان معیار فارسی",
     "tts_voice": "fa-IR-DilaraNeural"},
    {"key": "ckb", "label": "کردی سورانی", "lang_code": "ckb",
     "style": "کردی سورانی با گرامر و قواعد زبانی دقیق و درست",
     "tts_voice": None},  # صدای رایگان معتبر کردی سورانی پیدا نشد؛ فقط متن/زیرنویس
]


def translate_segments(segments, style, target_lang=DEFAULT_TRANSLATE_LANG):
    """سعی می‌کنه متن هر بخش رو با حفظ هم‌ترازی زمان‌بندی ترجمه کنه. اگه هم‌ترازی
    به‌هم بخوره (تعداد بخش‌ها فرق کنه)، None برمی‌گردونه."""
    joined = " ||| ".join(seg["text"].strip() for seg in segments)
    prompt = (
        f"متن زیر با جداکننده‌ی ' ||| ' به {len(segments)} بخش تقسیم شده. هر بخش رو روان و طبیعی "
        f"به زبان با کد {target_lang} ترجمه کن — لحن رو با نوع محتوا هماهنگ کن: اگه محتوا رسمی/اداری/خبری/آموزشیه، "
        f"از فارسی رسمی و درست استفاده کن؛ اگه محاوره‌ای و صمیمیه (مکالمه‌ی روزمره، شبکه‌های اجتماعی)، از لحن "
        f"محاوره‌ای طبیعی (نه رسمی و کتابی، ولی نه شعاری) استفاده کن. از کلمات رباتیک و ترجمه‌ی کلمه‌به‌کلمه "
        f"پرهیز کن. دقیقاً با همون جداکننده‌ی ' ||| ' بین بخش‌های ترجمه‌شده جدا کن. تعداد بخش‌های خروجی باید "
        f"دقیقاً {len(segments)} تا باشه، بدون هیچ توضیح اضافه:\n\n{joined}"
    )
    try:
        reply, _, _ = get_ai_response([{"role": "user", "content": prompt}], max_tokens=4000)
    except AllProvidersFailed:
        return None
    parts = [p.strip() for p in reply.split("|||")]
    if len(parts) != len(segments):
        return None
    return parts


def send_document(chat_id, filename, content_str):
    try:
        requests.post(
            f"{API_URL}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": (filename, content_str.encode("utf-8"))},
            timeout=30,
        )
    except Exception as e:
        print(f"خطا در ارسال فایل {filename}: {e}")


def handle_media_message(chat_id, media):
    file_size = media.get("file_size", 0)
    if file_size and file_size > MAX_MEDIA_BYTES:
        send_message(chat_id, "این فایل از ~۱۹ مگابایت بزرگ‌تره — سقف دانلود فایل بات‌های تلگرامه. "
                               "یه کلیپ کوتاه‌تر یا فقط فایل صوتی بفرست.")
        return
    if not GROQ_API_KEY:
        send_message(chat_id, "برای ترجمه‌ی ویدیو به GROQ_API_KEY نیاز دارم که هنوز تنظیم نشده.")
        return

    send_message(chat_id, "دریافت شد، در حال پردازش... (بسته به طول فایل ممکنه چند دقیقه طول بکشه)")

    try:
        file_info = requests.get(f"{API_URL}/getFile", params={"file_id": media["file_id"]}, timeout=30).json()
        file_path = file_info["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        raw_bytes = requests.get(file_url, timeout=120).content
    except Exception as e:
        send_message(chat_id, f"خطا در دانلود فایل: {e}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input_media")
        audio_path = os.path.join(tmpdir, "audio.mp3")
        with open(input_path, "wb") as f:
            f.write(raw_bytes)

        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-vn", "-acodec", "libmp3lame", "-q:a", "5", audio_path],
                check=True, timeout=180, capture_output=True,
            )
        except Exception as e:
            send_message(chat_id, f"خطا تو استخراج صدا (ffmpeg): {e}")
            return

        try:
            segments, detected_lang = transcribe_audio(audio_path)
        except Exception as e:
            send_message(chat_id, f"خطا تو تشخیص گفتار (Groq Whisper): {e}")
            return

    if not segments:
        send_message(chat_id, "هیچ گفتاری تو فایل تشخیص داده نشد.")
        return

    send_message(chat_id, f"زبان تشخیص داده شده: {detected_lang}. متن اصلی آماده شد، در حال ترجمه...")
    send_document(chat_id, "original.srt", build_srt(segments))
    send_document(chat_id, "original.txt", build_txt(segments))

    for style in TRANSLATION_STYLES:
        translated_parts = translate_segments_styled(segments, style)
        if not translated_parts:
            send_message(chat_id, f"⚠️ ترجمه‌ی سبک «{style['label']}» هم‌تراز نشد، ردش می‌کنم.")
            continue

        translated_segments = [
            {"start": s["start"], "end": s["end"], "text": t}
            for s, t in zip(segments, translated_parts)
        ]
        suffix = style["suffix"]
        send_document(chat_id, f"translated_{suffix}.srt", build_srt(translated_segments))

        if style["key"] == "fa_colloquial":
            send_document(chat_id, f"translated_{suffix}.vtt", build_vtt(translated_segments))
            send_document(chat_id, f"translated_{suffix}.sbv", build_sbv(translated_segments))

            natural_text = translate_full_text_natural(build_txt(segments), style["instruction"])
            final_text = natural_text or build_txt(translated_segments)
            send_document(chat_id, f"translated_{suffix}.txt", final_text)

            try:
                with tempfile.TemporaryDirectory() as tts_tmpdir:
                    audio_out_path = os.path.join(tts_tmpdir, "voice_fa.mp3")
                    text_to_speech_fa(final_text, audio_out_path)
                    send_audio_file(chat_id, audio_out_path,
                                     caption="صدای فارسی — دقیقاً همون متنِ فایل txt همین سبک")
            except Exception as e:
                print(f"خطا در تولید صدای فارسی: {e}")
                send_message(chat_id, "متن ترجمه آماده شد ولی تولید صدای فارسی موفق نشد.")
        else:
            send_document(chat_id, f"translated_{suffix}.txt", build_txt(translated_segments))

def fetch_channel_posts_simple(channel, limit=100):
    """پست‌های اخیر یه کانال عمومی رو برای خلاصه‌سازی می‌گیره (بدون نیاز به عضویت)."""
    url = f"https://t.me/s/{channel}"
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        text_div = msg.select_one(".tgme_widget_message_text")
        if text_div:
            posts.append(text_div.get_text("\n", strip=True))
    return posts[-limit:]

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
    # اول اسم‌های رایج (base_url, baseURL, api_base, invoke_url, endpoint و...) رو امتحان کن
    url_match = re.search(
        r'(?:base_url|baseURL|api_base|invoke_url|endpoint|api_endpoint)["\']?\s*[=:]\s*["\']([^"\']+)["\']',
        text)
    if not url_match:
        # اگه پیدا نشد، دنبال هر URLی بگرد که شبیه endpoint هوش مصنوعیه
        url_match = re.search(r'["\']?(https?://[^\s"\']+/(?:chat/completions|v\d+[^\s"\']*))["\']?', text)
    if not url_match:
        # آخرین تلاش: هر URL که تو کد باشه
        url_match = re.search(r'["\'](https?://[^\s"\']+)["\']', text)

    key_match = re.search(r'(?:api_key|apiKey|Authorization)["\']?\s*[=:]\s*["\']?(?:Bearer\s+)?([A-Za-z0-9\-_./]{15,})["\']?', text)
    model_match = re.search(r'\bmodel["\']?\s*[=:]\s*["\']([^"\']+)["\']', text)

    if not url_match or not key_match:
        return None

    key = key_match.group(1)
    if any(bad in key.upper() for bad in ["YOUR", "XXXX", "API_KEY", "TOKEN_HERE", "HERE"]):
        return None

    base_url = url_match.group(1)
    # اگه لینک کامل تا chat/completions بود، اون بخش رو قطع کن که فقط base_url بمونه
    base_url = re.sub(r'/chat/completions/?$', '', base_url)
    model = model_match.group(1) if model_match else None

    host = re.sub(r'^https?://', '', base_url).split('/')[0]
    domain_parts = [p for p in host.split('.') if p not in ("api", "www", "integrate", "inference")]
    name = domain_parts[0] if domain_parts else "custom"

    return {"name": name, "base_url": base_url, "model": model, "api_key": key, "tags": ["general"]}


def send_message(chat_id, text, reply_markup=None):
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        chunk = text[i:i + TELEGRAM_MAX_LEN]
        payload = {"chat_id": chat_id, "text": chunk}
        if reply_markup is not None and i == 0:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
        except Exception as e:
            print(f"خطا در ارسال پیام: {e}")


# دکمه‌های منوی اصلی — به‌جای تایپ دستور، لمس می‌کنی
MAIN_MENU_KEYBOARD = {
    "keyboard": [
        ["📖 راهنما", "🔄 شروع دوباره"],
        ["📊 وضعیت", "🧩 سرویس‌ها"],
        ["💰 چک‌ها", "🎨 ساخت عکس"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

MENU_LABEL_TO_COMMAND = {
    "📖 راهنما": "/help",
    "🔄 شروع دوباره": "/reset",
    "📊 وضعیت": "/status",
    "🧩 سرویس‌ها": "/providers",
    "💰 چک‌ها": "/checks",
    "🎨 ساخت عکس": "/help",  # چون /image به خودی خودش پرامپت لازم داره، راهنما رو نشون بده
}


AI_HORDE_BASE = "https://aihorde.net/api/v2"

# ---------------------------------------------------------------------------
# منوی چندسطحی با دکمه‌ی شیشه‌ای (Inline Keyboard)
# ---------------------------------------------------------------------------
MENU_PAGE_SIZE = 6


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API_URL}/editMessageText", json=payload, timeout=15)
    except Exception as e:
        print(f"خطا در ادیت پیام: {e}")


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{API_URL}/answerCallbackQuery", json=payload, timeout=15)
    except Exception as e:
        print(f"خطا در answerCallbackQuery: {e}")


def build_main_menu_inline():
    rows = [
        [{"text": "🤖 انتخاب سرویس چت", "callback_data": "nav:providers:0"}],
        [{"text": "🎨 راهنمای ساخت عکس", "callback_data": "info:image"}],
        [{"text": "📹 راهنمای ترجمه ویدیو", "callback_data": "info:video"}],
        [{"text": "💰 چک‌ها", "callback_data": "info:checks"}],
    ]
    return {"inline_keyboard": rows}


def build_provider_menu(data, chat_id, page=0):
    """لیست همه‌ی providerها (ثابت‌های کد + هرچی خودت اضافه کردی)، با صفحه‌بندی."""
    providers = BUILTIN_PROVIDERS + data.get("_providers", [])
    total_pages = max(1, (len(providers) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * MENU_PAGE_SIZE
    chunk = providers[start:start + MENU_PAGE_SIZE]

    current = data.get("_preferred_provider", {}).get(chat_id)
    rows = []
    for p in chunk:
        mark = "✅ " if p["name"] == current else ""
        rows.append([{"text": f"{mark}{p['name']}", "callback_data": f"sel:{p['name']}"}])

    nav_row = []
    if page > 0:
        nav_row.append({"text": "◀️ قبلی", "callback_data": f"nav:providers:{page - 1}"})
    if page < total_pages - 1:
        nav_row.append({"text": "بعدی ▶️", "callback_data": f"nav:providers:{page + 1}"})
    if nav_row:
        rows.append(nav_row)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "nav:main:0"}])

    text = f"سرویس چت رو انتخاب کن (صفحه {page + 1}/{total_pages}):\n" \
           f"سرویس فعلی: {current or 'پیش‌فرض (اولین سرویس فعال)'}"
    return {"inline_keyboard": rows}, text


def handle_callback_query(cq):
    callback_id = cq["id"]
    chat_id = str(cq["message"]["chat"]["id"])
    message_id = cq["message"]["message_id"]
    data_str = cq.get("data", "")
    answer_callback(callback_id)

    data = load_all()

    if data_str.startswith("nav:"):
        _, target, page_str = data_str.split(":")
        page = int(page_str)
        if target == "main":
            edit_message(chat_id, message_id, "منوی اصلی:", build_main_menu_inline())
        elif target == "providers":
            keyboard, text = build_provider_menu(data, chat_id, page)
            edit_message(chat_id, message_id, text, keyboard)
        return

    if data_str.startswith("sel:"):
        provider_name = data_str[len("sel:"):]
        preferred = data.get("_preferred_provider", {})
        preferred[chat_id] = provider_name
        data["_preferred_provider"] = preferred
        save_all(data)
        keyboard, text = build_provider_menu(data, chat_id, 0)
        edit_message(chat_id, message_id, f"✅ سرویس «{provider_name}» به‌عنوان اولویت اول ثبت شد.\n\n" + text, keyboard)
        return

    if data_str.startswith("info:"):
        topic = data_str[len("info:"):]
        info_texts = {
            "image": "برای ساخت عکس بنویس:\n/image توضیح عکس",
            "video": "فقط یه ویدیو، صدا، یا ویس بفرست (بدون هیچ دستوری) — خودم پردازشش می‌کنم.",
            "checks": "برای دیدن چک‌ها: /checks\nبرای ثبت جدید: /addcheck (جزئیات با /help)",
        }
        back_keyboard = {"inline_keyboard": [[{"text": "🏠 منوی اصلی", "callback_data": "nav:main:0"}]]}
        edit_message(chat_id, message_id, info_texts.get(topic, "..."), back_keyboard)
        return


def generate_image_ai_horde(prompt, api_key="0000000000", model="stable_diffusion"):
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "params": {"width": 512, "height": 512, "steps": 20, "n": 1},
        "models": [model],
    }
    resp = requests.post(f"{AI_HORDE_BASE}/generate/async", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    request_id = resp.json()["id"]

    for _ in range(60):  # حداکثر ۵ دقیقه صبر (۶۰ × ۵ ثانیه)
        time.sleep(5)
        check = requests.get(f"{AI_HORDE_BASE}/generate/check/{request_id}", timeout=15).json()
        if check.get("done"):
            break
        if check.get("faulted"):
            raise RuntimeError("تولید عکس با خطا مواجه شد")
    else:
        raise RuntimeError("زمان زیادی طول کشید (صف شلوغه)، دوباره امتحان کن")

    status = requests.get(f"{AI_HORDE_BASE}/generate/status/{request_id}", timeout=15).json()
    generations = status.get("generations", [])
    if not generations:
        raise RuntimeError("عکسی برنگشت")
    return generations[0]["img"]


def handle_update(update):
    message = update.get("message")
    if not message:
        return
    chat_id = str(message["chat"]["id"])
    chat_type = message["chat"].get("type", "private")

    media = message.get("video") or message.get("audio") or message.get("voice")
    doc = message.get("document")
    if not media and doc:
        mime = doc.get("mime_type") or ""
        if mime.startswith(("video/", "audio/")):
            media = doc
    if media:
        handle_media_message(chat_id, media)
        return

    if message.get("photo"):
        caption = (message.get("caption") or "").strip()
        if caption.startswith("/addcheck"):
            handle_addcheck(chat_id, caption, photo_file_id=message["photo"][-1]["file_id"])
        else:
            send_message(chat_id,
                "اگه می‌خوای این عکس چک رو با اطلاعاتش ثبت کنم، تو کپشن عکس بنویس:\n"
                "/addcheck ۱۴۰۵-۰۵-۲۴ توضیح")
        return

    if "text" not in message:
        return
    user_text = message["text"].strip()
    if user_text in MENU_LABEL_TO_COMMAND:
        user_text = MENU_LABEL_TO_COMMAND[user_text]

    # تو گروه/کانال، فقط پیام‌های عادی رو تو یه بافر جمع می‌کنیم (بدون جواب دادن)،
    # مگر این‌که دستور خاصی مثل /summarize باشه.
    if chat_type in ("group", "supergroup") and not user_text.startswith("/"):
        data = load_all()
        buffer = data.get("_group_buffer", {})
        chat_buf = buffer.get(chat_id, [])
        sender = (message.get("from") or {}).get("first_name", "ناشناس")
        chat_buf.append(f"{sender}: {user_text}")
        buffer[chat_id] = chat_buf[-MAX_GROUP_BUFFER:]
        data["_group_buffer"] = buffer
        save_all(data)
        return

    if user_text.startswith("/summarize"):
        parts = user_text.split(maxsplit=1)
        target = parts[1].strip().lstrip("@") if len(parts) > 1 else None

        if target:
            try:
                texts = fetch_channel_posts_simple(target)
            except Exception as e:
                send_message(chat_id, f"خطا در خوندن کانال «{target}»: {e}")
                return
        else:
            data = load_all()
            texts = data.get("_group_buffer", {}).get(chat_id, [])

        if not texts:
            send_message(chat_id, "پیامی برای خلاصه کردن پیدا نشد.")
            return

        joined = "\n".join(texts[-150:])
        prompt = (
            "پیام‌های زیر رو بخون. موضوع‌ها یا خبرهای تکراری/مشابه رو یکی کن (حذف تکراری)، "
            "و خروجی رو به‌صورت یه لیست خلاصه و مرتب بده (هر خط یه موضوع، بدون تکرار):\n\n" + joined
        )
        try:
            summary, _, _ = get_ai_response([{"role": "user", "content": prompt}], max_tokens=1500)
            send_message(chat_id, summary)
        except AllProvidersFailed:
            send_message(chat_id, "الان هیچ سرویس هوش مصنوعی در دسترس نیست.")

        if not target:
            data = load_all()
            buf = data.get("_group_buffer", {})
            buf[chat_id] = []
            data["_group_buffer"] = buf
            save_all(data)
        return

    if user_text.startswith("/register "):
        name = user_text[len("/register "):].strip()
        if not name:
            send_message(chat_id, "فرمت درست:\n/register اسم")
            return
        data = load_all()
        contacts = data.get("_contacts", {})
        contacts[name] = chat_id
        data["_contacts"] = contacts
        save_all(data)
        send_message(chat_id, f"ثبت شدی به اسم «{name}». حالا می‌شه یادآوری چک‌ها رو برات فوروارد کرد.")
        return

    if user_text.startswith("/addcheck"):
        handle_addcheck(chat_id, user_text)
        return

    if user_text == "/checks":
        data = load_all()
        my_checks = [c for c in data.get("_checks", []) if c["chat_id"] == chat_id]
        if not my_checks:
            send_message(chat_id, "هیچ چکی ثبت نشده.")
            return
        lines = [f"#{c['id']} — {c['due_jalali']} — {c['desc'] or '(بدون توضیح)'}"
                 + (" 📷" if c.get("photo_file_id") else "") for c in my_checks]
        send_message(chat_id, "\n".join(lines))
        return

    if user_text.startswith("/removecheck "):
        check_id = user_text[len("/removecheck "):].strip()
        data = load_all()
        checks = [c for c in data.get("_checks", []) if c["id"] != check_id]
        data["_checks"] = checks
        save_all(data)
        send_message(chat_id, f"چک #{check_id} حذف شد (اگه بود).")
        return

    if user_text.startswith("/addimagekey "):
        key = user_text[len("/addimagekey "):].strip()
        data = load_all()
        data["_image_api_key"] = key
        save_all(data)
        send_message(chat_id, "کلید تولید عکس (AI Horde) ذخیره شد.")
        return

    if user_text.startswith("/image "):
        prompt = user_text[len("/image "):].strip()
        data = load_all()
        api_key = data.get("_image_api_key", "0000000000")
        send_message(chat_id, "در حال تولید عکس... (بسته به شلوغی صف AI Horde ممکنه چند دقیقه طول بکشه)")
        try:
            image_url = generate_image_ai_horde(prompt, api_key=api_key)
        except Exception as e:
            send_message(chat_id, f"خطا در تولید عکس: {e}")
            return
        send_photo(chat_id, image_url, caption=prompt[:200])
        return

    if user_text == "/start":
        send_message(chat_id, "سلام! هرچی بخوای می‌تونی ازم بپرسی.", reply_markup=MAIN_MENU_KEYBOARD)
        return

    if user_text == "/menu":
        send_message(chat_id, "منو:", reply_markup=MAIN_MENU_KEYBOARD)
        return

    if user_text == "/models":
        keyboard = build_main_menu_inline()
        send_message(chat_id, "منوی اصلی (با دکمه‌ی زیر پیام):", reply_markup=keyboard)
        return

    if user_text == "/reset":
        data = load_all()
        data[chat_id] = []
        summaries = data.get("_history_summary", {})
        summaries.pop(chat_id, None)
        data["_history_summary"] = summaries
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
            "/disable name - خاموش کردن موقت یه سرویس (بدون حذف)\n"
            "/enable name - روشن کردن دوباره\n"
            "/setpriority name - این سرویس همیشه اول امتحان بشه\n"
            "/goal متن - ثبت هدف بلندمدت که تو همه‌ی جواب‌ها رعایت بشه\n"
            "/learn متن‌یا‌لینک - یادگیری یه سند یا سایت برای استفاده تو جواب‌های بعدی\n"
            "/forget - پاک کردن دانشی که با /learn دادی\n"
            "/moa سوال - گرفتن نظر همه‌ی سرویس‌ها و ترکیب بهترین جواب\n"
            "/skill add name متن - ساخت یه پیش‌تنظیم جدید\n"
            "/skill use name - فعال کردن یه اسکیل برای این چت\n"
            "/skill off - خاموش کردن اسکیل فعال\n"
            "/skill list - لیست اسکیل‌های ساخته‌شده\n"
            "/skill delete name - حذف یه اسکیل\n"
            "/help - همین راهنما\n"
            "/menu - نمایش دکمه‌های میانبر (کیبورد پایین صفحه)\n"
            "/models - منوی چندسطحی با دکمه‌ی شیشه‌ای برای انتخاب سرویس چت\n\n"
            "🎨 /addimagekey کلید - ثبت کلید AI Horde برای تولید عکس\n"
            "🎨 /image توضیح عکس - ساخت یه عکس از رو متن (رایگان، از AI Horde)\n\n"
            "📹 یه ویدیو، صدا، یا ویس (حداکثر ~۱۹ مگابایت) بفرست تا زبانش تشخیص داده بشه "
            "و متن اصلی + ترجمه‌ش رو به‌صورت srt/vtt/sbv/txt برات بفرستم.\n\n"
            "📋 /summarize - تو یه گروه (که باتم عضوشه)، پیام‌های اخیر رو خلاصه و بدون تکرار می‌ده\n"
            "📋 /summarize @channel - همین کار رو برای یه کانال عمومی انجام می‌ده\n\n"
            "💰 ثبت چک — این فرم رو (با مقادیر خودت) بفرست، با یا بدون عکس چک به‌عنوان کپشن:\n"
            "/addcheck\n"
            "تاریخ چک(روز٫ماه٫سال)شمسی: 24٫05٫1405\n"
            "مبلغ چک به عدد (ریال): 50000000\n"
            "مبلغ چک به حروف(ریال): پنجاه میلیون ریال\n"
            "شماره چک: 123456\n"
            "وجه شخص گیرنده: علی احمدی\n"
            "نام چک بانک: ملت\n"
            "ارسال به: اسم‌مخاطب (اختیاری، برای فوروارد یادآوری)\n"
            "💰 /checks - لیست چک‌های ثبت‌شده\n"
            "💰 /removecheck id - حذف یه چک\n"
            "👤 /register اسم - طرف مقابل این رو به بات می‌فرسته تا بتونی یادآوری‌ها رو براش فوروارد کنی")
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
        added = data.get("_providers", [])
        lines = []
        for p in added:
            status = "🔴 خاموش" if p.get("enabled") is False else "🟢 روشن"
            lines.append(f"{p['name']} — {status}")
        text = "providerهای اضافه‌شده:\n" + ("\n".join(lines) if lines else "هیچی")
        text += "\n\n(به‌علاوه‌ی gemini و groq که تو کد ثابتن)"
        send_message(chat_id, text)
        return

    if user_text.startswith("/disable "):
        name = user_text[len("/disable "):].strip()
        data = load_all()
        providers = data.get("_providers", [])
        found = False
        for p in providers:
            if p["name"] == name:
                p["enabled"] = False
                found = True
        if not found:
            send_message(chat_id, f"provider «{name}» پیدا نشد.")
            return
        data["_providers"] = providers
        save_all(data)
        send_message(chat_id, f"provider «{name}» خاموش شد (بدون حذف — هروقت خواستی /enable {name} بزن).")
        return

    if user_text.startswith("/enable "):
        name = user_text[len("/enable "):].strip()
        data = load_all()
        providers = data.get("_providers", [])
        found = False
        for p in providers:
            if p["name"] == name:
                p["enabled"] = True
                found = True
        if not found:
            send_message(chat_id, f"provider «{name}» پیدا نشد.")
            return
        data["_providers"] = providers
        save_all(data)
        send_message(chat_id, f"provider «{name}» دوباره روشن شد.")
        return

    if user_text.startswith("/setpriority "):
        name = user_text[len("/setpriority "):].strip()
        data = load_all()
        providers = data.get("_providers", [])
        target = [p for p in providers if p["name"] == name]
        if not target:
            send_message(chat_id, f"provider «{name}» پیدا نشد (فقط providerهای خودت اضافه‌شده قابل اولویت‌بندی‌ان).")
            return
        others = [p for p in providers if p["name"] != name]
        data["_providers"] = target + others
        save_all(data)
        send_message(chat_id, f"provider «{name}» اولویت اول شد، همیشه اول امتحان می‌شه.")
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

        content = normalize_text(arg)[:MAX_KNOWLEDGE_CHARS]
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

    if user_text.startswith("/skill"):
        parts = user_text.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else None

        if sub == "add" and len(parts) == 3:
            name_and_text = parts[2].split(maxsplit=1)
            if len(name_and_text) != 2:
                send_message(chat_id, "فرمت درست:\n/skill add name متنِ دستورالعمل")
                return
            name, instructions = name_and_text
            data = load_all()
            skills = data.get("_skills", {})
            skills[name] = instructions
            data["_skills"] = skills
            save_all(data)
            send_message(chat_id, f"اسکیل «{name}» ساخته شد. با /skill use {name} فعالش کن.")
            return

        if sub == "use" and len(parts) == 3:
            name = parts[2].strip()
            data = load_all()
            skills = data.get("_skills", {})
            if name not in skills:
                send_message(chat_id, f"اسکیلی به اسم «{name}» پیدا نشد. با /skill list ببین چی داری.")
                return
            active = data.get("_active_skill", {})
            active[chat_id] = name
            data["_active_skill"] = active
            save_all(data)
            send_message(chat_id, f"اسکیل «{name}» برای این چت فعال شد.")
            return

        if sub == "off":
            data = load_all()
            active = data.get("_active_skill", {})
            active.pop(chat_id, None)
            data["_active_skill"] = active
            save_all(data)
            send_message(chat_id, "اسکیل فعال خاموش شد.")
            return

        if sub == "list":
            data = load_all()
            names = list(data.get("_skills", {}).keys())
            active = data.get("_active_skill", {}).get(chat_id)
            text = "اسکیل‌های ساخته‌شده: " + (", ".join(names) if names else "هیچی")
            if active:
                text += f"\n\nفعال الان: {active}"
            send_message(chat_id, text)
            return

        if sub == "delete" and len(parts) == 3:
            name = parts[2].strip()
            data = load_all()
            skills = data.get("_skills", {})
            skills.pop(name, None)
            data["_skills"] = skills
            save_all(data)
            send_message(chat_id, f"اسکیل «{name}» حذف شد (اگه بود).")
            return

        send_message(chat_id,
            "فرمت درست:\n"
            "/skill add name متن\n"
            "/skill use name\n"
            "/skill off\n"
            "/skill list\n"
            "/skill delete name")
        return

    if not user_text.startswith("/") and any(kw in user_text for kw in
            ["base_url", "baseURL", "api_base", "api_key", "apiKey", "Authorization",
             "invoke_url", "endpoint", "chat/completions"]):
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
    history = compact_history(chat_id, data)
    history.append({"role": "user", "content": user_text})

    try:
        system_prompt = SYSTEM_PROMPT
        history_summary = data.get("_history_summary", {}).get(chat_id)
        if history_summary:
            system_prompt += f"\n\nخلاصه‌ی بخش قدیمی‌تر همین مکالمه (برای حفظ زمینه):\n{history_summary}"
        goal = data.get("_goal")
        if goal:
            system_prompt += f"\n\nهدف بلندمدت کاربر که باید در پاسخ‌ها بهش پایبند باشی: {goal}"
        knowledge = data.get("_knowledge")
        if knowledge:
            system_prompt += f"\n\nاطلاعات زمینه‌ای که کاربر قبلاً بهت داده (در صورت ربط به سوال ازش استفاده کن):\n{knowledge}"

        active_skill = data.get("_active_skill", {}).get(chat_id)
        if active_skill:
            skill_instructions = data.get("_skills", {}).get(active_skill)
            if skill_instructions:
                system_prompt += f"\n\nاسکیل فعال «{active_skill}» — این دستورالعمل رو دقیق دنبال کن:\n{skill_instructions}"

        for url in data.get("_learned_urls", []):
            try:
                live_text = fetch_url_text(url)
                system_prompt += f"\n\nمحتوای زنده‌ی {url} (تازه، همین الان خونده شده):\n{live_text}"
            except Exception as e:
                print(f"خطا در خوندن لینک یادگرفته‌شده {url}: {e}")

        messages = [{"role": "system", "content": system_prompt}] + history
        extra_providers = data.get("_providers", [])

        preferred_name = data.get("_preferred_provider", {}).get(chat_id)
        if preferred_name:
            all_named = BUILTIN_PROVIDERS + extra_providers
            preferred_match = next((p for p in all_named if p["name"] == preferred_name), None)
            if preferred_match:
                extra_providers = [preferred_match] + [p for p in extra_providers if p["name"] != preferred_name]

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


def send_photo(chat_id, file_id, caption=""):
    try:
        requests.post(f"{API_URL}/sendPhoto",
                      json={"chat_id": chat_id, "photo": file_id, "caption": caption},
                      timeout=15)
    except Exception as e:
        print(f"خطا در ارسال عکس: {e}")


PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
CHECK_FIELD_MAP = [
    ("تاریخ چک", "date"),
    ("مبلغ چک به عدد", "amount_number"),
    ("مبلغ چک به حروف", "amount_words"),
    ("شماره چک", "check_number"),
    ("وجه شخص گیرنده", "payee"),
    ("نام چک بانک", "bank"),
    ("نام بانک", "bank"),
]


def normalize_digits(s):
    return s.translate(str.maketrans(PERSIAN_DIGITS, "0123456789"))


def parse_jalali_date(raw):
    """روز٫ماه٫سال یا سال٫ماه٫روز، با هر جداکننده‌ای (٫ ، / - فاصله) — سال جلالی
    همیشه بالای ۳۱ه، پس ترتیب رو خودکار تشخیص می‌دیم."""
    raw = normalize_digits(raw)
    for sep in ["٫", "،", "/", ",", " "]:
        raw = raw.replace(sep, "-")
    raw = re.sub(r"-+", "-", raw).strip("-")
    parts = [p for p in raw.split("-") if p]
    if len(parts) != 3:
        raise ValueError("فرمت تاریخ نامشخص")
    nums = list(map(int, parts))
    if nums[0] > 31:
        year, month, day = nums
    else:
        day, month, year = nums
    due_gregorian = jdatetime.date(year, month, day).togregorian()
    return due_gregorian, f"{year}-{month:02d}-{day:02d}"


def parse_check_form(text):
    """خط تاریخ و خط «ارسال به» (اختیاری) رو جدا می‌کنه؛ بقیه‌ی خط‌ها دست‌نخورده
    برای توضیح نگه داشته می‌شن. مقدار هرکدوم می‌تونه سرِ همون خط باشه یا خط بعدی."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    date_value = None
    forward_name = None
    remaining = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if date_value is None and (line.startswith("تاریخ چک") or line.startswith("تاریخ سررسید")):
            if ":" in line and line.split(":", 1)[1].strip():
                date_value = line.split(":", 1)[1].strip()
            elif i + 1 < len(lines):
                date_value = lines[i + 1].strip()
                i += 1
            i += 1
            continue
        if forward_name is None and (line.startswith("ارسال به") or line.startswith("فوروارد به") or line.startswith("گیرنده دوم")):
            if ":" in line and line.split(":", 1)[1].strip():
                forward_name = line.split(":", 1)[1].strip()
            elif i + 1 < len(lines):
                forward_name = lines[i + 1].strip()
                i += 1
            i += 1
            continue
        remaining.append(line)
        i += 1
    return date_value, forward_name, remaining


def handle_addcheck(chat_id, command_text, photo_file_id=None):
    body = command_text[len("/addcheck"):].strip()
    date_value, forward_name, remaining_lines = parse_check_form(body)

    forward_chat_id = None
    if forward_name:
        data = load_all()
        forward_chat_id = data.get("_contacts", {}).get(forward_name)
        if not forward_chat_id:
            send_message(chat_id,
                f"«{forward_name}» تو مخاطب‌ها پیدا نشد — طرف باید اول خودش /register {forward_name} "
                f"رو به همین بات بفرسته، بعد دوباره چک رو ثبت کن.")
            return

    if date_value:
        try:
            due_gregorian, due_jalali = parse_jalali_date(date_value)
        except Exception:
            send_message(chat_id, "تاریخ چک رو نتونستم بخونم. فرمت: روز٫ماه٫سال (مثلاً 24٫05٫1405)")
            return
        desc = "\n".join(remaining_lines).strip()
    else:
        parts = body.split(maxsplit=1)
        if not parts:
            send_message(chat_id,
                "فرمت درست:\n"
                "تاریخ چک(روز٫ماه٫سال)شمسی: 24٫05٫1405\n"
                "مبلغ چک به عدد (ریال): 50000000\n"
                "مبلغ چک به حروف(ریال): پنجاه میلیون ریال\n"
                "شماره چک: 123456\n"
                "وجه شخص گیرنده: علی احمدی\n"
                "نام چک بانک: ملت\n"
                "ارسال به: اسم‌مخاطب (اختیاری)")
            return
        try:
            due_gregorian, due_jalali = parse_jalali_date(parts[0])
        except Exception:
            send_message(chat_id, "فرمت تاریخ درست نیست. مثال: /addcheck 1405-05-24 توضیح")
            return
        desc = parts[1] if len(parts) > 1 else ""

    data = load_all()
    checks = data.get("_checks", [])
    new_id = str(max([int(c["id"]) for c in checks], default=0) + 1)
    checks.append({
        "id": new_id,
        "chat_id": chat_id,
        "forward_chat_id": forward_chat_id,
        "due": due_gregorian.isoformat(),
        "due_jalali": due_jalali,
        "desc": desc,
        "photo_file_id": photo_file_id,
        "reminded_date": None,
    })
    data["_checks"] = checks
    save_all(data)
    extra = " (با عکس)" if photo_file_id else ""
    extra += f"، فوروارد به «{forward_name}»" if forward_chat_id else ""
    send_message(chat_id, f"چک #{new_id} ثبت شد{extra}، سررسید {due_jalali}.\n{desc}")


def check_reminders():
    """هر چک که فردا سررسیدشه رو، اگه امروز هنوز یادآوری نشده، یه پیام (و عکس در صورت وجود) می‌فرسته.
    چک‌هایی که تاریخشون گذشته، خودکار از حافظه حذف می‌شن."""
    data = load_all()
    checks = data.get("_checks", [])
    if not checks:
        return
    today = datetime.utcnow().date()
    changed = False
    remaining = []
    for c in checks:
        try:
            due = datetime.strptime(c["due"], "%Y-%m-%d").date()
        except ValueError:
            remaining.append(c)
            continue

        days_left = (due - today).days

        if days_left < 0:
            changed = True
            continue  # سررسید گذشته، حذفش کن

        if days_left == 1 and c.get("reminded_date") != str(today):
            text = f"⏰ یادآوری: فردا ({c.get('due_jalali', c['due'])}) سررسید چک «{c.get('desc') or 'بدون توضیح'}» هست."
            recipients = [c["chat_id"]]
            if c.get("forward_chat_id"):
                recipients.append(c["forward_chat_id"])
            for rid in recipients:
                if c.get("photo_file_id"):
                    send_photo(rid, c["photo_file_id"], caption=text)
                else:
                    send_message(rid, text)
            c["reminded_date"] = str(today)
            changed = True

        remaining.append(c)

    if changed:
        data["_checks"] = remaining
        save_all(data)


def main():
    # هر webhook قدیمی که رو این توکن مونده باشه رو پاک می‌کنیم، وگرنه
    # getUpdates با خطای 409 Conflict برخورد می‌کنه.
    try:
        requests.get(f"{API_URL}/deleteWebhook", timeout=15)
    except Exception as e:
        print(f"خطا در deleteWebhook: {e}")

    start_time = time.time()
    offset = None
    last_reminder_check = 0
    print("بات روشن شد...")

    while time.time() - start_time < MAX_RUNTIME_SECONDS:
        if time.time() - last_reminder_check > 3600:
            try:
                check_reminders()
            except Exception as e:
                print(f"خطا در چک یادآوری چک‌ها: {e}")
            last_reminder_check = time.time()

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
                if update.get("callback_query"):
                    handle_callback_query(update["callback_query"])
                else:
                    handle_update(update)
            except Exception as e:
                print(f"خطا در پردازش پیام: {e}")

    print("زمان این اجرا تموم شد؛ اجرای بعدی طبق کرون شروع می‌شه.")


if __name__ == "__main__":
    main()
