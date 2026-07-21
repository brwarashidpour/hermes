"""
storage.py
----------
تاریخچه‌ی مکالمه‌ها رو تو یه فایل JSON داخل GitHub Gist نگه می‌داره، ولی با
کش در حافظه: خوندن از Gist فقط یک بار در شروع انجام می‌شه و نوشتن‌ها debounce
می‌شن (حداکثر هر ۲۰ ثانیه یک بار). نتیجه: هر پیام دیگه ۲ تا رفت‌وبرگشت HTTP
به GitHub نداره — هم خیلی سریع‌تر، هم rate limit گیت‌هاب مصرف نمی‌شه.
"""
import json
import time
import threading
import requests
from config import GIST_TOKEN, GIST_ID

GIST_API = f"https://api.github.com/gists/{GIST_ID}"
FILE_NAME = "history.json"
HEADERS = {
    "Authorization": f"token {GIST_TOKEN}",
    "Accept": "application/vnd.github+json",
}
SAVE_INTERVAL_SECONDS = 20   # حداکثر هر ۲۰ ثانیه یک بار واقعاً تو Gist نوشته می‌شه

_lock = threading.RLock()
_cache = None
_dirty = False
_last_save = 0.0


def _fetch_remote() -> dict:
    resp = requests.get(GIST_API, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    content = resp.json()["files"].get(FILE_NAME, {}).get("content", "")
    return json.loads(content) if content.strip() else {}


def _write_remote() -> None:
    global _dirty, _last_save
    payload = {
        "files": {FILE_NAME: {"content": json.dumps(_cache, ensure_ascii=False, indent=2)}}
    }
    resp = requests.patch(GIST_API, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    _dirty = False
    _last_save = time.time()


def load_all() -> dict:
    """بار اول از Gist می‌خونه؛ بعد از اون همیشه از کش حافظه — بدون رفت‌وبرگشت شبکه."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _fetch_remote()
        return _cache


def save_all(data: dict) -> None:
    """تغییرات رو تو کش می‌ذاره و اگه از آخرین نوشتن به اندازه‌ی کافی گذشته باشه،
    واقعاً تو Gist می‌نویسه (debounce). برای تضمین نوشتن، flush() صدا بزن."""
    global _cache, _dirty
    with _lock:
        _cache = data
        _dirty = True
        if time.time() - _last_save >= SAVE_INTERVAL_SECONDS:
            try:
                _write_remote()
            except Exception as e:
                print(f"خطا در ذخیره‌ی Gist (تغییرات تو کش می‌مونه و بعداً دوباره تلاش می‌شه): {e}")


def flush() -> None:
    """هر تغییر ذخیره‌نشده رو همین الان تو Gist می‌نویسه (پایان اجرا / فواصل منظم)."""
    with _lock:
        if _dirty:
            _write_remote()
