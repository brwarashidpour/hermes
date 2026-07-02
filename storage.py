"""
storage.py
----------
تاریخچه‌ی مکالمه‌ی هر کاربر رو تو یه فایل JSON داخل یه GitHub Gist نگه
می‌داره، تا حافظه‌ی بات بعد از هر ری‌استارت (هر ۶ ساعت) از بین نره.
"""
import json
import requests
from config import GIST_TOKEN, GIST_ID

GIST_API = f"https://api.github.com/gists/{GIST_ID}"
FILE_NAME = "history.json"
HEADERS = {
    "Authorization": f"token {GIST_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def load_all() -> dict:
    """تاریخچه‌ی همه‌ی کاربرها رو از Gist می‌خونه: {chat_id: [messages]}"""
    resp = requests.get(GIST_API, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    content = resp.json()["files"].get(FILE_NAME, {}).get("content", "")
    return json.loads(content) if content.strip() else {}


def save_all(data: dict) -> None:
    """کل دیکشنری تاریخچه رو برمی‌گردونه تو Gist."""
    payload = {
        "files": {FILE_NAME: {"content": json.dumps(data, ensure_ascii=False, indent=2)}}
    }
    resp = requests.patch(GIST_API, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
