import re
from pathlib import Path


EXTRA_BLOCKLIST_SUBSTRINGS = [
    "正品",
    "正版",
    "高仿",
    "水果",
    "手機",
    "emby",
]

MAX_LINKS = 3
MIN_VIDEO_DURATION_SECONDS = 5

DISALLOWED_DOC_EXTS = {".txt", ".pdf"}
DISALLOWED_DOC_MIMES = {"text/plain", "application/pdf"}

URL_RE = re.compile(r"(?i)(?:\bhttps?://|\bwww\.|\bt\.me/)\S+")
PUNCT_ONLY_RE = re.compile(r"[\s\-–—.,;:!?()\[\]{}<>\"'“”‘’]+")


def is_blocked(text: str, blocklist_substrings: list[str] | None = None) -> bool:
    hay = str(text or "").casefold()
    for s in (blocklist_substrings or []) + EXTRA_BLOCKLIST_SUBSTRINGS:
        if not s:
            continue
        if str(s).casefold() in hay:
            return True
    return False


def count_links(text: str | None) -> int:
    if not text:
        return 0
    return len(list(URL_RE.finditer(str(text))))


def has_too_many_links(text: str | None, *, max_links: int = MAX_LINKS) -> bool:
    return count_links(text) > max_links


def is_link_only(text: str | None) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    rest = URL_RE.sub(" ", s)
    rest = PUNCT_ONLY_RE.sub(" ", rest)
    return not rest.strip()


def document_meta_text(msg) -> str:
    parts: list[str] = []

    doc = getattr(msg, "document", None)
    if doc is not None:
        mime = str(getattr(doc, "mime_type", "") or "")
        if mime:
            parts.append(mime)
        for attr in getattr(doc, "attributes", []) or []:
            fn = getattr(attr, "file_name", None)
            if fn:
                parts.append(str(fn))
            alt = getattr(attr, "alt", None)
            if alt:
                parts.append(str(alt))
            title = getattr(attr, "title", None)
            if title:
                parts.append(str(title))
            performer = getattr(attr, "performer", None)
            if performer:
                parts.append(str(performer))

    media = getattr(msg, "media", None)
    wp = getattr(media, "webpage", None)
    if wp is not None:
        for key in ("url", "site_name", "title", "description"):
            value = getattr(wp, key, None)
            if value:
                parts.append(str(value))

    return "\n".join([p for p in parts if str(p).strip()])


def filter_haystack(msg_text: str, msg) -> str:
    parts = [msg_text]
    meta = document_meta_text(msg)
    if meta:
        parts.append(meta)
    return "\n".join([p for p in parts if str(p).strip()])


def is_gif_or_sticker(msg, *, telethon_types) -> bool:
    if getattr(msg, "gif", None) is not None:
        return True
    if getattr(msg, "sticker", None) is not None:
        return True

    doc = getattr(msg, "document", None)
    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, telethon_types.DocumentAttributeSticker):
            return True
        if isinstance(attr, telethon_types.DocumentAttributeAnimated):
            return True
    return False


def is_video_message(msg) -> bool:
    return bool(getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "round_video", None))


def video_duration_seconds(msg, *, telethon_types) -> int | None:
    doc = getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "round_video", None)
    if doc is None:
        doc = getattr(msg, "document", None)

    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, telethon_types.DocumentAttributeVideo):
            try:
                return int(getattr(attr, "duration", 0) or 0)
            except Exception:  # noqa: BLE001
                return None

    return None


def is_short_video(msg, *, telethon_types, min_duration_seconds: int = MIN_VIDEO_DURATION_SECONDS) -> bool:
    if not is_video_message(msg):
        return False
    duration = video_duration_seconds(msg, telethon_types=telethon_types)
    return duration is not None and duration < min_duration_seconds


def is_photo_message(msg) -> bool:
    if getattr(msg, "photo", None) is not None:
        return True

    doc = getattr(msg, "document", None)
    mime = str(getattr(doc, "mime_type", "") or "")
    if mime.startswith("image/") and not is_video_message(msg):
        return True

    return False


def is_location_message(msg, *, telethon_types) -> bool:
    media = getattr(msg, "media", None)
    if media is None:
        return False
    return isinstance(media, (telethon_types.MessageMediaGeo, telethon_types.MessageMediaGeoLive, telethon_types.MessageMediaVenue))


def is_disallowed_document(
    msg,
    *,
    disallowed_doc_mimes: set[str] = DISALLOWED_DOC_MIMES,
    disallowed_doc_exts: set[str] = DISALLOWED_DOC_EXTS,
) -> bool:
    doc = getattr(msg, "document", None)
    if doc is None:
        return False

    mime = str(getattr(doc, "mime_type", "") or "").casefold()
    if mime in disallowed_doc_mimes:
        return True

    for attr in getattr(doc, "attributes", []) or []:
        fn = getattr(attr, "file_name", None)
        if not fn:
            continue
        ext = Path(str(fn)).suffix.lower()
        if ext in disallowed_doc_exts:
            return True

    return False
