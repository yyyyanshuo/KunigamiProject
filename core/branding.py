"""Public product names, independent of historical internal identifiers."""

BRAND_NAMES = {"ja": "咲き", "en": "Saki", "zh-CN": "花期"}
SUPPORT_EMAIL = "yyyyanshuo@foxmail.com"


def brand_name(language="ja"):
    language = str(language or "ja").replace("_", "-").lower()
    if language.startswith("zh"):
        return BRAND_NAMES["zh-CN"]
    return BRAND_NAMES.get(language.split("-")[0], BRAND_NAMES["ja"])


def request_brand_name():
    from core.localization import user_language
    return brand_name(user_language())
