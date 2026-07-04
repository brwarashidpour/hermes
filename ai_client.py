"""
ai_client.py
------------
لایه‌ی یکپارچه برای صحبت با چند سرویس هوش مصنوعی مختلف.
providerها به ترتیب لیست PROVIDERS امتحان می‌شن؛ اولین کدوم که جواب داد
همون برگردونده می‌شه. اگه یکی rate-limit بخوره یا از کار بیفته، خودکار
میره سراغ بعدی — بدون اینکه بات از کار بیفته.

نیازمندی: pip install requests
"""

import os
import logging
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_client")

PROVIDERS = [
    {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",
        "tags": ["general"],
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "tags": ["general", "fast"],
    },
]

TIMEOUT_SECONDS = 30


class ProviderAuthError(Exception):
    """کلید نامعتبره یا اعتبار/توکنش تموم شده — یعنی دیگه اصلاً کار نمی‌کنه."""
    pass


class AllProvidersFailed(RuntimeError):
    """هیچ providerی جواب نداد. dead_providers یعنی کدوماشون کلیدشون س
