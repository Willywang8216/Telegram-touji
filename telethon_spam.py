from __future__ import annotations

from typing import Iterable

from telethon import types


def extract_urls_and_mentions(text: str, entities: Iterable[object] | None) -> tuple[list[str], list[str]]:
    text = text or ""
    urls: list[str] = []
    mentions: list[str] = []

    for ent in entities or []:
        if isinstance(ent, types.MessageEntityTextUrl):
            if getattr(ent, "url", None):
                urls.append(str(ent.url))
            continue

        if isinstance(ent, types.MessageEntityUrl):
            try:
                urls.append(text[int(ent.offset) : int(ent.offset) + int(ent.length)])
            except Exception:  # noqa: BLE001
                continue
            continue

        if isinstance(ent, types.MessageEntityMention):
            try:
                mentions.append(text[int(ent.offset) : int(ent.offset) + int(ent.length)])
            except Exception:  # noqa: BLE001
                continue
            continue

    return urls, mentions


_PROMO_KEYWORDS = [
    "promo",
    "promot",
    "promoting",
    "best channels",
    "autodelete",
    "auto delete",
    "duration",
    "lista",
]

_PRICE_KEYWORDS = [
    "buy",
    "order",
    "vip",
    "price",
    "usd",
    "usdt",
    "￥",
    "$",
    "元",
    "下单",
    "下單",
    "咨询",
    "諮詢",
    "特惠",
    "優惠",
    "活动",
    "活動",
    "价格",
    "價格",
    "买一送一",
    "買一送一",
    "会员",
    "會員",
]


def looks_like_promo_directory(text: str, urls: Iterable[str] | None = None, mentions: Iterable[str] | None = None) -> bool:
    """Heuristic to block directory/promoting spam.

    This targets messages that contain lots of channel links (often hidden under MessageEntityTextUrl).
    It is intentionally conservative to avoid blocking normal porn captions that might contain 1-2 links.
    """

    text = (text or "").strip()
    urls = [str(u) for u in (urls or []) if u]
    mentions = [str(m) for m in (mentions or []) if m]

    norm = text.lower()
    line_count = len([ln for ln in norm.splitlines() if ln.strip()])

    tme_links = [u for u in urls if ("t.me/" in u.lower()) or ("telegram.me/" in u.lower())]
    link_count = len(urls)
    mention_count = len(mentions)

    if link_count >= 8:
        return True

    if len(tme_links) >= 5:
        return True

    if (link_count + mention_count) >= 10 and line_count >= 6:
        return True

    has_promo_kw = any(k in norm for k in _PROMO_KEYWORDS)
    if has_promo_kw and (link_count + mention_count) >= 3:
        return True

    has_price_kw = any(k.lower() in norm for k in _PRICE_KEYWORDS)
    if has_price_kw and (mention_count > 0 or len(tme_links) > 0):
        return True

    return False


def message_looks_like_promo_directory(msg) -> bool:
    text = (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "")
    urls, mentions = extract_urls_and_mentions(text, getattr(msg, "entities", None))
    return looks_like_promo_directory(text, urls=urls, mentions=mentions)


def group_looks_like_promo_directory(msgs: list) -> bool:
    texts: list[str] = []
    urls: list[str] = []
    mentions: list[str] = []

    for m in msgs or []:
        t = (getattr(m, "raw_text", None) or getattr(m, "message", None) or "")
        if t:
            texts.append(str(t))
        u, me = extract_urls_and_mentions(str(t or ""), getattr(m, "entities", None))
        urls.extend(u)
        mentions.extend(me)

    return looks_like_promo_directory("\n".join(texts), urls=urls, mentions=mentions)
