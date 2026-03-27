import re
from dataclasses import dataclass
from typing import Union
from urllib.parse import urlparse


_TG_HOST_RE = re.compile(r"(^|\.)t\.me$|(^|\.)telegram\.me$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedMessageLink:
    chat: Union[int, str]
    message_id: int
    topic_id: int | None = None


def looks_like_message_link(value: str) -> bool:
    s = (value or "").strip()
    return "t.me/" in s or "telegram.me/" in s


def parse_message_link(value: str) -> ParsedMessageLink | None:
    s = (value or "").strip().strip("<>")
    if not s:
        return None

    # Telegram clients sometimes include trailing punctuation or direction marks
    # when users copy-paste links (e.g. ".../12345)" or ".../12345\u200e").
    s = s.rstrip(").,;:!?]}'\"”’›»\u200e\u200f\u202a\u202b\u202c\u202d\u202e")

    if s.startswith("t.me/") or s.startswith("telegram.me/"):
        s = "https://" + s

    u = urlparse(s)
    if not u.netloc:
        return None

    host = u.netloc.split(":", 1)[0].lower()
    if not _TG_HOST_RE.search(host):
        return None

    parts = [p for p in (u.path or "").split("/") if p]
    if len(parts) < 2:
        return None

    if parts[0] == "c":
        if len(parts) < 3:
            return None
        internal = parts[1]
        if not internal.isdigit():
            return None
        chat: int | str = int(f"-100{internal}")
        nums = parts[2:]
    else:
        chat = parts[0]
        nums = parts[1:]

    if not nums:
        return None

    digits = [n for n in nums if n.isdigit()]
    if len(digits) != len(nums):
        return None

    ids = [int(n) for n in nums]
    message_id = ids[-1]
    topic_id = ids[-2] if len(ids) >= 2 else None

    if isinstance(chat, str) and chat.startswith("@"):  # pragma: no cover
        chat = chat[1:]

    return ParsedMessageLink(chat=chat, message_id=message_id, topic_id=topic_id)
