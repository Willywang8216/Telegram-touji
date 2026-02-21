import re
from typing import Iterable


DEFAULT_CONTACT_AD_KEYWORDS = [
    "私信",
    "联系",
    "下单",
    "咨询",
    "购买",
    "付款",
    "支付",
    "价格",
    "价",
    "特惠",
    "优惠",
    "活动",
    "买一送一",
    "会员",
    "vip",
    "私密",
    "频道",
    "入群",
    "加入",
    "群",
    "备用群",
    "客服",
    "代理",
    "推广",
    "usdt",
    "btc",
    "eth",
    "支付宝",
    "微信",
    "qq",
    "vx",
    "￥",
    "元",
    "$",
]


def _normalize_text(text: str) -> str:
    return str(text).lower()


def _compact_text(text: str) -> str:
    return "".join(_normalize_text(text).split())


def contains_blocked_substring(text: str, needles: Iterable[str]) -> bool:
    if not text:
        return False

    norm = _normalize_text(text)
    compact = _compact_text(text)

    for needle in needles or []:
        if not needle:
            continue
        n = _normalize_text(needle)
        if n in norm:
            return True
        if _compact_text(n) in compact:
            return True

    return False


def matches_any_regex(text: str, patterns: Iterable[str]) -> bool:
    if not text:
        return False
    for pat in patterns or []:
        if not pat:
            continue
        if re.search(pat, text, flags=re.IGNORECASE | re.DOTALL):
            return True
    return False


def looks_like_contact_ad(text: str, keywords: Iterable[str] | None = None) -> bool:
    if not text:
        return False

    norm = _normalize_text(text)

    has_username = re.search(r"@[a-z0-9_]{4,}", norm) is not None
    has_tme = ("t.me/" in norm) or ("telegram.me/" in norm) or ("tg://" in norm)
    has_contact = has_username or has_tme

    kws = [_normalize_text(k) for k in (keywords or DEFAULT_CONTACT_AD_KEYWORDS) if k]
    has_keyword = any(k and k in norm for k in kws)

    if has_contact and has_keyword:
        return True

    if "备用群" in norm and has_tme:
        return True

    has_price = re.search(r"(￥|\$|\b\d+\s*(?:元|rmb|usd|usdt)\b)", norm) is not None
    if has_price and any(k in norm for k in ["会员", "vip", "购买", "下单", "咨询", "价格", "优惠", "特惠"]):
        return True

    return False


def should_block_text(
    text: str,
    *,
    blocklist_substrings: Iterable[str] | None = None,
    blocklist_regexes: Iterable[str] | None = None,
    block_contact_ads: bool = True,
    contact_ad_keywords: Iterable[str] | None = None,
) -> bool:
    if not text:
        return False

    if contains_blocked_substring(text, blocklist_substrings or []):
        return True

    if matches_any_regex(text, blocklist_regexes or []):
        return True

    if block_contact_ads and looks_like_contact_ad(text, keywords=contact_ad_keywords):
        return True

    return False
