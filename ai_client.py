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

# ---------------------------------------------------------------------------
# providerها به ترتیب اولویت. هر کدوم باید یه endpoint سازگار با
# "OpenAI Chat Completions" داشته باشه (یعنی مسیر .../chat/completions با
# فرمت استاندارد messages/model). امروز تقریباً همه‌ی سرویس‌ها همینو ساپورت
# می‌کنن، پس اضافه کردن provider جدید فقط یعنی یه آیتم جدید به این لیست.
# ---------------------------------------------------------------------------
PROVIDERS = [
    {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",   # gemini-3.5-flash هم جدیدتره، می‌تونی امتحان کنی
        "tags": ["general"],
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",   # کیفیت بالاتر، سقف ۱۰۰۰ درخواست/روز
        "tags": ["general", "fast"],
        # اگه سقف روزانه مهم‌تر از کیفیته: "llama-3.1-8b-instant" (۱۴۴۰۰ درخواست/روز)
    },
    # provider جدید (ZenMux، Babel Town، هرچی) رو با همین ساختار اضافه کن:
    # {
    #     "name": "...",
    #     "base_url": "https://.../v1",
    #     "api_key_env": "...",
    #     "tags": ["general"],
    # },
]

TIMEOUT_SECONDS = 30


class ProviderAuthError(Exception):
    """کلید نامعتبره یا اعتبار/توکنش تموم شده — یعنی دیگه اصلاً کار نمی‌کنه."""
    pass


class AllProvidersFailed(RuntimeError):
    """هیچ providerی جواب نداد. dead_providers یعنی کدوماشون کلیدشون سوخته."""
    def __init__(self, message, dead_providers):
        super().__init__(message)
        self.dead_providers = dead_providers


def _ask_provider(provider: dict, messages: list, max_tokens: int) -> str:
    """یک provider رو صدا می‌زنه. اگه مشکلی پیش بیاد، exception می‌ندازه بالا."""
    api_key = provider.get("api_key") or os.environ.get(provider.get("api_key_env", ""))
    if not api_key:
        raise ValueError("کلید API این provider موجود نیست")

    url = f"{provider['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": provider["model"],
        "messages": messages,
        "max_tokens": max_tokens,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT_SECONDS)

    if resp.status_code == 429:
        raise RuntimeError("rate limit خورد (429)")
    if resp.status_code in (401, 402, 403):
        raise ProviderAuthError(f"کلید نامعتبر یا اعتبار تموم شده ({resp.status_code})")
    if resp.status_code == 404:
        raise ProviderAuthError("آدرس یا مدل پیدا نشد (404) — احتمالاً base_url یا اسم مدل اشتباهه")
    resp.raise_for_status()

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("جواب خالی برگشت")

    return content.strip()


def ask_single_provider(provider: dict, messages: list, max_tokens: int = 1000) -> str:
    """یه provider مشخص رو بدون هیچ fallbacky صدا می‌زنه؛ برای /moa که نظر تک‌تک providerها لازمه."""
    return _ask_provider(provider, messages, max_tokens)


def get_ai_response(messages: list, max_tokens: int = 1000, extra_providers: list = None, topic: str = None):
    """
    messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
    extra_providers: providerهای اضافه‌ای که در زمان اجرا (مثلاً از تلگرام) اضافه شدن.
    topic: اگه بدی (مثلاً "code")، providerهایی که این تگ رو دارن اول امتحان می‌شن،
        بقیه به‌عنوان fallback بعدش میان.
    خروجی: تاپل (متن_جواب, اسم_provider_ی که جواب داد, لیست_providerهای_سوخته)
    اگه همه‌ی providerها شکست بخورن، AllProvidersFailed می‌ده (با dead_providers).
    """
    all_providers = (extra_providers or []) + PROVIDERS
    if topic:
        matched = [p for p in all_providers if topic in p.get("tags", [])]
        others = [p for p in all_providers if p not in matched]
        providers = matched + others
    else:
        providers = all_providers

    failures = []
    dead_providers = []
    for provider in providers:
        if provider.get("enabled") is False:
            continue
        try:
            answer = _ask_provider(provider, messages, max_tokens)
            logger.info(f"جواب از provider: {provider['name']}")
            return answer, provider["name"], dead_providers
        except ProviderAuthError as e:
            logger.warning(f"{provider['name']} کلیدش سوخته: {e}")
            failures.append(f"{provider['name']}: {e}")
            dead_providers.append(provider["name"])
            continue
        except Exception as e:
            logger.warning(f"{provider['name']} شکست خورد: {e}")
            failures.append(f"{provider['name']}: {e}")
            continue

    raise AllProvidersFailed("همه‌ی providerها شکست خوردن:\n" + "\n".join(failures), dead_providers)


# ---------------------------------------------------------------------------
# تست مستقیم روی خود گوشی/ترمینال: python ai_client.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_messages = [
        {"role": "user", "content": "به فارسی، در یک جمله بگو کی هستی."}
    ]
    try:
        answer, used, _ = get_ai_response(test_messages)
        print(f"\n[provider: {used}]\n{answer}")
    except RuntimeError as e:
        print(f"\nخطا: {e}")
