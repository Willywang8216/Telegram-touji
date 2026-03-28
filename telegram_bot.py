import asyncio
import logging
import re
import shlex
import tempfile
import time
from pathlib import Path

from twitter_watch import ensure_parent_dir, list_profile_tweets, tweet_id_from_url

from telethon import TelegramClient, functions, types, utils
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError, MessageIdInvalidError
from telethon.events import NewMessage
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest

from command_utils import parse_command
from common_config import ConfigManager, load_userbot_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from telegram_link_utils import looks_like_message_link, parse_message_link
from twitter_expand import extract_tweet_urls
from route_filter_utils import filter_routes, parse_route_filters

try:
    from structured_logger import get_logger, log_event
except ModuleNotFoundError:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    def log_event(logger: logging.Logger, level: int, message: str, **kwargs):
        logger.log(level, message)


logger = get_logger("userbot")
config_manager = ConfigManager()
settings = load_userbot_settings(config_manager)

# Keyed by (chat_id, grouped_id) to avoid collisions across different source chats.
media_group_cache: dict[tuple[int, int], dict] = {}
media_group_lock = asyncio.Lock()

client = TelegramClient("anon", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
forwarding_map: dict[int, object] = {}
bot_mappings = settings["bot_mappings"]
blocklist_substrings = (config_manager.load().get("relay", {}) or {}).get("blocklist_substrings", []) or []
rate_limiter = AsyncRateLimiter(rate_per_sec=8)
DLQ_PATH = "logs/userbot_dlq.jsonl"
_MAX_LINKS = 3
_MIN_VIDEO_DURATION_SECONDS = 5

_DISALLOWED_DOC_EXTS = {".txt", ".pdf"}
_DISALLOWED_DOC_MIMES = {"text/plain", "application/pdf"}

_URL_RE = re.compile(r"(?i)(?:\bhttps?://|\bwww\.|\bt\.me/)\S+")


def _compute_ignored_source_chats(cfg: dict) -> set[int]:
    relay = cfg.get("relay", {}) or {}
    out: set[int] = set()

    for x in relay.get("dest_channels", []) or []:
        try:
            out.add(int(x))
        except Exception:  # noqa: BLE001
            continue

    for d in relay.get("default_destinations", []) or []:
        try:
            out.add(int(d.get("chat_id")))
        except Exception:  # noqa: BLE001
            continue

    for r in relay.get("routes", []) or []:
        for d in r.get("destinations", []) or []:
            try:
                out.add(int(d.get("chat_id")))
            except Exception:  # noqa: BLE001
                continue

    for x in relay.get("ignore_source_chats", []) or []:
        try:
            out.add(int(x))
        except Exception:  # noqa: BLE001
            continue

    return out


ignored_source_chats = _compute_ignored_source_chats(config_manager.load())


def _is_blocked(text: str) -> bool:
    hay = str(text or "").casefold()
    for s in (blocklist_substrings or []) + _EXTRA_BLOCKLIST_SUBSTRINGS:
        if not s:
            continue
        if str(s).casefold() in hay:
            return True
    return False


def _count_links(text: str | None) -> int:
    if not text:
        return 0
    return len(list(_URL_RE.finditer(str(text))))


def _has_too_many_links(text: str | None) -> bool:
    return _count_links(text) > _MAX_LINKS


def _is_link_only(text: str | None) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    rest = _URL_RE.sub(" ", s)
    rest = re.sub(r"[\s\-–—.,;:!?()\[\]{}<>\"'“”‘’]+", " ", rest)
    return not rest.strip()


def _document_meta_text(msg) -> str:
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
        for k in ("url", "site_name", "title", "description"):
            v = getattr(wp, k, None)
            if v:
                parts.append(str(v))

    return "\n".join([p for p in parts if str(p).strip()])


def _filter_haystack(msg_text: str, msg) -> str:
    parts = [msg_text]
    meta = _document_meta_text(msg)
    if meta:
        parts.append(meta)
    return "\n".join([p for p in parts if str(p).strip()])


def _is_gif_or_sticker(msg) -> bool:
    if getattr(msg, "gif", None) is not None:
        return True
    if getattr(msg, "sticker", None) is not None:
        return True

    doc = getattr(msg, "document", None)
    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, types.DocumentAttributeSticker):
            return True
        if isinstance(attr, types.DocumentAttributeAnimated):
            return True
    return False


def _is_video_message(msg) -> bool:
    return bool(getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "round_video", None))


def _video_duration_seconds(msg) -> int | None:
    doc = getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "round_video", None)
    if doc is None:
        doc = getattr(msg, "document", None)

    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, types.DocumentAttributeVideo):
            try:
                return int(getattr(attr, "duration", 0) or 0)
            except Exception:  # noqa: BLE001
                return None

    return None


def _is_short_video(msg) -> bool:
    if not _is_video_message(msg):
        return False
    dur = _video_duration_seconds(msg)
    return dur is not None and dur < _MIN_VIDEO_DURATION_SECONDS


def _is_photo_message(msg) -> bool:
    if getattr(msg, "photo", None) is not None:
        return True

    doc = getattr(msg, "document", None)
    mime = str(getattr(doc, "mime_type", "") or "")
    if mime.startswith("image/") and not _is_video_message(msg):
        return True

    return False


def _is_location_message(msg) -> bool:
    media = getattr(msg, "media", None)
    if media is None:
        return False
    return isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue))


def _is_disallowed_document(msg) -> bool:
    doc = getattr(msg, "document", None)
    if doc is None:
        return False

    mime = str(getattr(doc, "mime_type", "") or "").casefold()
    if mime in _DISALLOWED_DOC_MIMES:
        return True

    for attr in getattr(doc, "attributes", []) or []:
        fn = getattr(attr, "file_name", None)
        if not fn:
            continue
        ext = Path(str(fn)).suffix.lower()
        if ext in _DISALLOWED_DOC_EXTS:
            return True

    return False


# Prefix used when we cannot forward (e.g. protected content / noforwards) or when we need
# to preserve routing metadata (source chat/topic) across a copy.
# Relay bot will parse it and use it for routing, then strip it from outgoing captions/text.
_SOURCE_CHAT_ID_MARKER_PREFIX = "\u2063SRC_CHAT_ID="
_SOURCE_TOPIC_ID_MARKER_PREFIX = "\u2063SRC_TOPIC_ID="

_EXTRA_BLOCKLIST_SUBSTRINGS = [
    "正品",
    "正版",
    "高仿",
    "水果",
    "手機",
    "emby",
]

_TWITTER_WATCH_DLQ_PATH = "logs/twitter_watch_dlq.jsonl"


def _with_source_marker(source_chat_id: int, source_topic_id: int | None, text: str | None) -> str:
    out = f"{_SOURCE_CHAT_ID_MARKER_PREFIX}{int(source_chat_id)}\n"
    if source_topic_id is not None:
        out += f"{_SOURCE_TOPIC_ID_MARKER_PREFIX}{int(source_topic_id)}\n"
    return out + (text or "")


def update_config_file(new_bot_mappings):
    global bot_mappings
    cfg = config_manager.load()
    bot_mappings = new_bot_mappings
    cfg["bot_mappings"] = new_bot_mappings
    config_manager.save(cfg)
    log_event(logger, logging.INFO, "config_updated", mapping_count=len(new_bot_mappings))
    asyncio.create_task(rebuild_forwarding_map())


async def rebuild_forwarding_map():
    global forwarding_map
    forwarding_map = {}
    for mapping in bot_mappings:
        source_chat = mapping["source_chat"]
        target_bot = mapping["target_bot"]
        try:
            try:
                src_id = int(source_chat)
            except ValueError:
                src_id = source_chat
            source_entity = await client.get_entity(src_id)
            target_entity = await client.get_entity(str(target_bot))

            # Safety: prevent misconfiguration where target_bot is actually a channel/group.
            # If this happens, the *user account* will forward directly into that channel/group.
            if not getattr(target_entity, "bot", False):
                log_event(
                    logger,
                    logging.ERROR,
                    "mapping_failed_target_not_bot",
                    source_chat=str(source_chat),
                    target_bot=str(target_bot),
                )
                continue

            peer_id = int(utils.get_peer_id(source_entity))
            forwarding_map[peer_id] = target_entity
            log_event(logger, logging.INFO, "mapping_updated", source_chat=str(source_chat), target_bot=str(target_bot))
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "mapping_failed", source_chat=str(source_chat), error=str(exc))


def _read_archive_ids(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    out: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s:
            out.add(s)
    return out


def _append_archive_id(path: str | Path, tweet_id: str) -> None:
    ensure_parent_dir(path)
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(str(tweet_id).strip() + "\n")


async def _poll_twitter_watch_source(src: dict, *, cookies_file: str | None, bot_cache: dict[str, object]) -> None:
    profile = (src.get("profile") or src.get("account") or src.get("url") or "").strip()
    if not profile:
        return

    try:
        source_chat_id = int(src.get("source_chat_id"))
    except Exception:  # noqa: BLE001
        return

    target_bot = str(src.get("target_bot") or "").strip()
    if not target_bot.startswith("@"):  # keep it explicit
        return

    interval = int(src.get("poll_interval_sec", 300) or 300)
    interval = max(30, interval)

    fetch_limit = int(src.get("fetch_limit", 30) or 30)
    fetch_limit = max(1, min(fetch_limit, 200))

    archive_file = str(src.get("archive_file") or f"state/twitter_watch_{source_chat_id}.txt")

    key = f"{target_bot}"
    if key not in bot_cache:
        ent = await client.get_entity(target_bot)
        if not getattr(ent, "bot", False):
            return
        bot_cache[key] = ent

    target_ent = bot_cache[key]

    seen = _read_archive_ids(archive_file)

    tweets = await asyncio.to_thread(list_profile_tweets, profile, cookies_file=cookies_file, limit=fetch_limit)
    if not tweets:
        log_event(logger, logging.INFO, "twitter_watch_empty", profile=str(profile), source_chat_id=source_chat_id)
        return

    pending: list[dict[str, str]] = []
    for t in tweets:
        url = str(t.get("url") or "")
        tid = tweet_id_from_url(url)
        if not tid or tid in seen:
            continue
        pending.append({"url": url, "text": str(t.get("text") or "")})

    if not pending:
        return

    sent = 0
    for t in reversed(pending):
        url = str(t.get("url") or "").strip()
        tid = tweet_id_from_url(url)
        if not url or not tid or tid in seen:
            continue

        text = str(t.get("text") or "").strip()
        body = (text + "\n" if text else "") + url
        marked_text = _with_source_marker(source_chat_id, None, body)

        await rate_limiter.wait()
        await with_retry(
            lambda: client.send_message(target_ent, marked_text),
            retries=3,
            base_delay=1,
            logger=logger,
            action="twitter_watch_send",
        )

        _append_archive_id(archive_file, tid)
        seen.add(tid)
        sent += 1

    log_event(
        logger,
        logging.INFO,
        "twitter_watch_sent",
        profile=str(profile),
        source_chat_id=source_chat_id,
        sent=sent,
        interval=interval,
    )


async def twitter_watch_loop() -> None:
    next_run: dict[str, float] = {}
    bot_cache: dict[str, object] = {}

    while True:
        try:
            cfg = config_manager.load()
            watch = cfg.get("twitter_watch", {}) or {}
            if not watch.get("enabled", False):
                await asyncio.sleep(10)
                continue

            sources = watch.get("sources", []) or []
            if not sources:
                await asyncio.sleep(10)
                continue

            cookies_file = watch.get("cookies_file")
            if not cookies_file:
                cookies_file = (cfg.get("relay", {}) or {}).get("twitter_cookies_file")
            if not cookies_file and Path("twitter.cookies.txt").exists():
                cookies_file = "twitter.cookies.txt"

            now = time.monotonic()
            for src in sources:
                if not isinstance(src, dict):
                    continue

                profile = str(src.get("profile") or src.get("account") or src.get("url") or "")
                if not profile:
                    continue

                try:
                    source_chat_id = int(src.get("source_chat_id"))
                except Exception:  # noqa: BLE001
                    continue

                interval = int(src.get("poll_interval_sec", 300) or 300)
                interval = max(30, interval)

                key = f"{source_chat_id}:{profile}"
                due = next_run.get(key, 0.0)
                if now < due:
                    continue

                next_run[key] = now + interval

                try:
                    await _poll_twitter_watch_source(src, cookies_file=cookies_file, bot_cache=bot_cache)
                except Exception as exc:  # noqa: BLE001
                    payload = {"profile": profile, "source_chat_id": source_chat_id, "error": str(exc)}
                    write_dlq(_TWITTER_WATCH_DLQ_PATH, payload)
                    log_event(logger, logging.ERROR, "twitter_watch_poll_failed", **payload)

            await asyncio.sleep(5)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": str(exc)}
            write_dlq(_TWITTER_WATCH_DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "twitter_watch_loop_failed", **payload)
            await asyncio.sleep(10)


def _extract_source_topic_top_id(msg) -> int | None:
    reply_to = getattr(msg, "reply_to", None)
    top = getattr(reply_to, "reply_to_top_id", None)
    if top:
        try:
            return int(top)
        except Exception:  # noqa: BLE001
            return None

    if getattr(msg, "is_topic", False):
        try:
            return int(getattr(msg, "id", 0) or 0) or None
        except Exception:  # noqa: BLE001
            return None

    return None


async def _send_copy_to_bot(target_bot, msg, *, source_chat_id: int, source_topic_id: int | None):
    text = getattr(msg, "raw_text", None) or getattr(msg, "text", None) or ""
    marked_text = _with_source_marker(int(source_chat_id), source_topic_id, text)

    media = getattr(msg, "media", None)
    if media is not None:
        tmp = tempfile.TemporaryDirectory(prefix="userbot_copy_")
        try:
            local_path = await client.download_media(msg, file=tmp.name)
            if not local_path:
                await with_retry(
                    lambda: client.send_message(target_bot, marked_text),
                    retries=3,
                    base_delay=1,
                    logger=logger,
                    action="copy_single_text_fallback",
                )
                return

            await with_retry(
                lambda: client.send_file(target_bot, local_path, caption=marked_text),
                retries=3,
                base_delay=1,
                logger=logger,
                action="copy_single_media",
            )
        finally:
            tmp.cleanup()
        return

    await with_retry(
        lambda: client.send_message(target_bot, marked_text),
        retries=3,
        base_delay=1,
        logger=logger,
        action="copy_single_text",
    )


async def safe_forward_single(
    target_bot,
    message_id,
    chat_id,
    *,
    noforwards: bool = False,
    force_copy: bool = False,
    source_topic_id: int | None = None,
    msg=None,
):
    await rate_limiter.wait()

    if (noforwards or force_copy) and msg is not None:
        try:
            await _send_copy_to_bot(target_bot, msg, source_chat_id=int(chat_id), source_topic_id=source_topic_id)
            log_event(logger, logging.INFO, "message_copied", chat_id=str(chat_id), message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
            write_dlq(DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "message_copy_failed", **payload)
        return

    try:
        await with_retry(
            lambda: client.forward_messages(target_bot, message_id, from_peer=chat_id),
            retries=3,
            base_delay=1,
            logger=logger,
            action="forward_single",
        )
        log_event(logger, logging.INFO, "message_forwarded", chat_id=str(chat_id), message_id=message_id)
    except (ChatForwardsRestrictedError, MessageIdInvalidError) as exc:
        if msg is not None:
            try:
                await _send_copy_to_bot(target_bot, msg, source_chat_id=int(chat_id), source_topic_id=source_topic_id)
                log_event(logger, logging.INFO, "message_copied_fallback", chat_id=str(chat_id), message_id=message_id)
                return
            except Exception as copy_exc:  # noqa: BLE001
                payload = {"chat_id": chat_id, "message_id": message_id, "error": str(copy_exc)}
                write_dlq(DLQ_PATH, payload)
                log_event(logger, logging.ERROR, "message_copy_failed", **payload)
                return

        payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "message_forward_failed", **payload)
    except Exception as exc:  # noqa: BLE001
        payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "message_forward_failed", **payload)


@client.on(NewMessage())
async def handler(event):
    if getattr(event, "out", False) or getattr(getattr(event, "message", None), "out", False):
        return

    if config_manager.reload_if_changed():
        global bot_mappings, blocklist_substrings, ignored_source_chats
        bot_mappings = load_userbot_settings(config_manager)["bot_mappings"]
        cfg = config_manager.load()
        blocklist_substrings = (cfg.get("relay", {}) or {}).get("blocklist_substrings", []) or []
        ignored_source_chats = _compute_ignored_source_chats(cfg)
        await rebuild_forwarding_map()
        log_event(logger, logging.INFO, "config_hot_reloaded")

    if event.chat_id is not None and int(event.chat_id) in ignored_source_chats:
        log_event(logger, logging.INFO, "message_skipped_ignored_source", chat_id=str(event.chat_id))
        return

    target_bot = forwarding_map.get(event.chat_id)
    if not target_bot:
        return

    msg_text = getattr(event.message, "raw_text", "") or ""
    filter_haystack = _filter_haystack(msg_text, event.message)

    is_gif_or_sticker = _is_gif_or_sticker(event.message)
    is_disallowed_doc = _is_disallowed_document(event.message)
    is_location = _is_location_message(event.message)
    is_blocked = bool(filter_haystack and _is_blocked(filter_haystack))
    too_many_links = _has_too_many_links(msg_text)
    is_short_video = _is_short_video(event.message)

    if not event.message.grouped_id:
        if is_gif_or_sticker:
            log_event(
                logger,
                logging.INFO,
                "message_skipped_gif_or_sticker",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
            )
            return

        if is_disallowed_doc:
            log_event(
                logger,
                logging.INFO,
                "message_skipped_disallowed_document",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
            )
            return

        if is_short_video:
            log_event(
                logger,
                logging.INFO,
                "message_skipped_short_video",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
                duration=_video_duration_seconds(event.message),
            )
            return

        if is_location:
            log_event(
                logger,
                logging.INFO,
                "message_skipped_location",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
            )
            return

        if getattr(event.message, "media", None) is not None and not msg_text.strip():
            log_event(
                logger,
                logging.INFO,
                "message_skipped_attachment_only",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
            )
            return

        if _is_link_only(msg_text) and not extract_tweet_urls(msg_text):
            log_event(
                logger,
                logging.INFO,
                "message_skipped_link_only",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
            )
            return

        if _is_photo_message(event.message):
            log_event(
                logger,
                logging.INFO,
                "message_skipped_single_image",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
            )
            return

        if is_blocked or too_many_links:
            log_event(
                logger,
                logging.INFO,
                "message_blocked",
                chat_id=str(event.chat_id),
                message_id=getattr(event.message, "id", None),
                links=_count_links(msg_text) if too_many_links else None,
            )
            return

    noforwards = bool(getattr(event.message, "noforwards", False))
    if not noforwards:
        try:
            chat = await event.get_chat()
            noforwards = bool(getattr(chat, "noforwards", False))
        except Exception:  # noqa: BLE001
            pass

    source_topic_id = _extract_source_topic_top_id(event.message)
    force_copy = source_topic_id is not None

    if event.message.grouped_id:
        async with media_group_lock:
            key = (event.chat_id, event.message.grouped_id)
            if key not in media_group_cache:
                media_group_cache[key] = {
                    "messages": [],
                    "task": None,
                    "target_bot": target_bot,
                    "noforwards": noforwards,
                    "blocked": False,
                    "source_topic_id": source_topic_id,
                }
            elif media_group_cache[key].get("source_topic_id") is None and source_topic_id is not None:
                media_group_cache[key]["source_topic_id"] = source_topic_id

            if is_gif_or_sticker or is_disallowed_doc or is_location or is_blocked or too_many_links:
                media_group_cache[key]["blocked"] = True

            media_group_cache[key]["messages"].append(event.message)
            if media_group_cache[key]["task"]:
                media_group_cache[key]["task"].cancel()
            media_group_cache[key]["task"] = asyncio.create_task(process_media_group(key))
        return

    await safe_forward_single(
        target_bot,
        event.message.id,
        event.chat_id,
        noforwards=noforwards,
        force_copy=force_copy,
        source_topic_id=source_topic_id,
        msg=event.message,
    )


async def process_media_group(key: tuple[int, int]):
    from_peer, grouped_id = key
    await asyncio.sleep(1.5)

    async with media_group_lock:
        data = media_group_cache.get(key)
        if not data:
            return
        del media_group_cache[key]

    msgs = data.get("messages") or []
    msgs.sort(key=lambda m: m.id)

    if data.get("blocked"):
        log_event(logger, logging.INFO, "group_blocked", chat_id=str(from_peer), group_id=grouped_id)
        return

    caption_check = next(
        (
            (getattr(m, "raw_text", None) or getattr(m, "text", None) or "")
            for m in msgs
            if (getattr(m, "raw_text", None) or getattr(m, "text", None))
        ),
        "",
    )

    meta_parts = [_document_meta_text(m) for m in msgs]
    filter_text = "\n".join([p for p in [caption_check, *meta_parts] if str(p).strip()])

    if any(_is_gif_or_sticker(m) for m in msgs):
        log_event(logger, logging.INFO, "group_skipped_gif_or_sticker", chat_id=str(from_peer), group_id=grouped_id)
        return

    if any(_is_disallowed_document(m) for m in msgs):
        log_event(logger, logging.INFO, "group_skipped_disallowed_document", chat_id=str(from_peer), group_id=grouped_id)
        return

    if any(_is_short_video(m) for m in msgs):
        log_event(
            logger,
            logging.INFO,
            "group_skipped_short_video",
            chat_id=str(from_peer),
            group_id=grouped_id,
        )
        return

    if any(_is_location_message(m) for m in msgs):
        log_event(
            logger,
            logging.INFO,
            "group_skipped_location",
            chat_id=str(from_peer),
            group_id=grouped_id,
        )
        return

    if any(getattr(m, "media", None) is not None for m in msgs) and not str(caption_check or "").strip():
        log_event(logger, logging.INFO, "group_skipped_attachment_only", chat_id=str(from_peer), group_id=grouped_id)
        return

    if _is_link_only(caption_check) and not extract_tweet_urls(caption_check or ""):
        log_event(logger, logging.INFO, "group_skipped_link_only", chat_id=str(from_peer), group_id=grouped_id)
        return

    if filter_text and (_is_blocked(filter_text) or _has_too_many_links(caption_check)):
        log_event(
            logger,
            logging.INFO,
            "group_blocked",
            chat_id=str(from_peer),
            group_id=grouped_id,
            links=_count_links(caption_check) if _has_too_many_links(caption_check) else None,
        )
        return

    source_topic_id = data.get("source_topic_id")
    force_copy = source_topic_id is not None

    if not any(_is_video_message(m) for m in msgs):
        photo_count = sum(1 for m in msgs if _is_photo_message(m))
        if photo_count == 1:
            log_event(logger, logging.INFO, "group_skipped_single_image", chat_id=str(from_peer), group_id=grouped_id)
            return

    try:
        if data.get("noforwards") or force_copy:
            caption = next(
                (
                    (getattr(m, "raw_text", None) or getattr(m, "text", None) or "")
                    for m in msgs
                    if (getattr(m, "raw_text", None) or getattr(m, "text", None))
                ),
                "",
            )
            caption = _with_source_marker(int(from_peer), source_topic_id, caption)

            tmp = tempfile.TemporaryDirectory(prefix="userbot_copy_group_")
            try:
                files: list[str] = []
                for m in msgs:
                    if getattr(m, "media", None) is None:
                        continue
                    p = await client.download_media(m, file=tmp.name)
                    if p:
                        files.append(str(p))

                if not files:
                    await with_retry(
                        lambda: client.send_message(data["target_bot"], caption),
                        retries=3,
                        base_delay=1,
                        logger=logger,
                        action="copy_group_text_fallback",
                    )
                    log_event(logger, logging.INFO, "group_copied_text_only", group_id=grouped_id, count=len(msgs))
                    return

                await rate_limiter.wait()
                await with_retry(
                    lambda: client.send_file(data["target_bot"], files, caption=caption),
                    retries=3,
                    base_delay=1,
                    logger=logger,
                    action="copy_group_media",
                )
                log_event(logger, logging.INFO, "group_copied", group_id=grouped_id, count=len(msgs))
            finally:
                tmp.cleanup()
            return

        await rate_limiter.wait()
        await with_retry(
            lambda: client.forward_messages(data["target_bot"], [m.id for m in msgs], from_peer=from_peer),
            retries=3,
            base_delay=1,
            logger=logger,
            action="forward_group",
        )
        log_event(logger, logging.INFO, "group_forwarded", group_id=grouped_id, count=len(msgs))
    except Exception as exc:  # noqa: BLE001
        payload = {"group_id": grouped_id, "messages": [m.id for m in msgs], "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "group_forward_failed", **payload)


async def join_chat(entity):
    await client(JoinChannelRequest(entity))


async def leave_chat(entity):
    await client(LeaveChannelRequest(entity))


def _parse_destinations(tokens: list[str]) -> list[dict]:
    out: list[dict] = []
    for tok in tokens:
        if not tok:
            continue
        if "@" in tok:
            chat_str, topic_str = tok.split("@", 1)
            out.append({"chat_id": int(chat_str), "topic_id": int(topic_str)})
            continue
        if "=" in tok:
            chat_str, title = tok.split("=", 1)
            title = title.strip()
            out.append({"chat_id": int(chat_str), "topic_title": title})
            continue
        out.append({"chat_id": int(tok)})
    return out


def _format_destinations(destinations: list[dict]) -> str:
    parts: list[str] = []
    for d in destinations or []:
        chat_id = d.get("chat_id")
        if d.get("topic_id") is not None:
            parts.append(f"{chat_id}@{d.get('topic_id')}")
        elif d.get("topic_title"):
            parts.append(f"{chat_id}=\"{d.get('topic_title')}\"")
        else:
            parts.append(str(chat_id))
    return " ".join(parts)


async def _resolve_message_link(link: str) -> tuple[int, int, int | None]:
    parsed = parse_message_link(link)
    if not parsed:
        raise ValueError("invalid_link")

    ent = await client.get_entity(parsed.chat)
    chat_id = int(utils.get_peer_id(ent))

    msg = await client.get_messages(ent, ids=int(parsed.message_id))
    if not msg:
        raise ValueError("message_not_found")

    topic_top = _extract_source_topic_top_id(msg)
    if topic_top is None and parsed.topic_id is not None:
        topic_top = int(parsed.topic_id)

    return chat_id, int(parsed.message_id), topic_top


async def _build_routes_report(routes: list[dict], args: str) -> str | None:
    entity_cache: dict[int, object] = {}
    topic_title_cache: dict[tuple[int, int], str] = {}

    async def _get_entity(chat_id: int):
        if chat_id in entity_cache:
            return entity_cache[chat_id]
        ent = await client.get_entity(chat_id)
        entity_cache[chat_id] = ent
        return ent

    def _entity_label(ent) -> str:
        title = getattr(ent, "title", None)
        username = getattr(ent, "username", None)
        if title and username:
            return f"{title} (@{username})"
        if title:
            return str(title)
        if username:
            return f"@{username}"
        return str(getattr(ent, "id", ""))

    def _internal_chat_id(chat_id: int) -> int | None:
        # t.me/c/<internal>/<msg_id> uses chat id without the -100 prefix.
        if str(int(chat_id)).startswith("-100"):
            return abs(int(chat_id)) - 1000000000000
        return None

    def _chat_link(ent, chat_id: int) -> str | None:
        username = getattr(ent, "username", None)
        if username:
            return f"https://t.me/{username}"
        internal = _internal_chat_id(chat_id)
        if internal is not None:
            return f"https://t.me/c/{internal}/1"
        return None

    def _message_link(ent, chat_id: int, msg_id: int) -> str | None:
        username = getattr(ent, "username", None)
        if username:
            return f"https://t.me/{username}/{int(msg_id)}"
        internal = _internal_chat_id(chat_id)
        if internal is not None:
            return f"https://t.me/c/{internal}/{int(msg_id)}"
        return None

    async def _topic_title(chat_id: int, top_message_id: int) -> str | None:
        if top_message_id == 1:
            return "General"

        key = (chat_id, top_message_id)
        if key in topic_title_cache:
            return topic_title_cache[key]

        ent = await _get_entity(chat_id)
        msg = await client.get_messages(ent, ids=int(top_message_id))
        if not msg:
            return None

        action = getattr(msg, "action", None)
        title = getattr(action, "title", None)
        if not title:
            return None

        topic_title_cache[key] = str(title)
        return str(title)

    filtered = list(routes)
    if (args or "").strip():
        flt = parse_route_filters(args or "")
        topic_sub = str(flt.get("topic") or "").casefold().strip() or None

        base = dict(flt)
        base["topic"] = None
        filtered = filter_routes(list(routes), filters=base)

        if topic_sub is not None:
            matched: list[dict] = []
            for r in filtered:
                ok = False
                for d in (r.get("destinations") or []):
                    if topic_sub in str(d.get("topic_title") or "").casefold():
                        ok = True
                        break

                    if d.get("topic_id") is not None and d.get("chat_id") is not None:
                        try:
                            title = await _topic_title(int(d.get("chat_id")), int(d.get("topic_id")))
                        except Exception:  # noqa: BLE001
                            title = None
                        if title and topic_sub in str(title).casefold():
                            ok = True
                            break

                if ok:
                    matched.append(r)
            filtered = matched

    if not filtered:
        return None

    keep = {id(r) for r in filtered}

    out: list[str] = [f"🤖 Routes ({len(filtered)}/{len(routes)}):"]
    if (args or "").strip():
        out.append(f"Filters: {args}")
    out.append("Tip: use /export_routes to always download a file.")

    for idx, r in enumerate(routes, start=1):
        if id(r) not in keep:
            continue

        out.append(f"\n{idx})")

        source_chats = [int(x) for x in (r.get("source_chats") or [])]
        source_topics = [int(x) for x in (r.get("source_topics") or [])]
        destinations = list(r.get("destinations") or [])

        out.append("  Sources:")
        for cid in source_chats:
            ent = None
            label = None
            url = None
            try:
                ent = await _get_entity(cid)
                label = _entity_label(ent)
                url = _chat_link(ent, cid)
            except Exception:  # noqa: BLE001
                ent = None

            line = f"    - {cid}"
            if label:
                line += f" | {label}"
            if url:
                line += f" | {url}"
            out.append(line)

            if source_topics:
                topic_parts: list[str] = []
                for tid in source_topics:
                    try:
                        title = await _topic_title(cid, tid)
                    except Exception:  # noqa: BLE001
                        title = None
                    part = str(tid)
                    if title:
                        part += f" | {title}"

                    if ent is not None:
                        turl = _message_link(ent, cid, tid)
                        if turl:
                            part += f" | {turl}"

                    topic_parts.append(part)
                out.append("      topics: " + "; ".join(topic_parts))
            else:
                try:
                    ent2 = ent or (entity_cache.get(cid) or await _get_entity(cid))
                    if getattr(ent2, "forum", False):
                        out.append("      topics: ALL")
                except Exception:  # noqa: BLE001
                    pass

        out.append("  Destinations:")
        for d in destinations:
            chat_id = int(d.get("chat_id"))
            ent = None
            chat_label = None
            chat_url = None
            try:
                ent = await _get_entity(chat_id)
                chat_label = _entity_label(ent)
                chat_url = _chat_link(ent, chat_id)
            except Exception:  # noqa: BLE001
                ent = None

            line = f"    - {chat_id}"
            if chat_label:
                line += f" | {chat_label}"
            if chat_url:
                line += f" | {chat_url}"

            if d.get("topic_id") is not None:
                topic_id = int(d.get("topic_id"))
                topic_label = None
                try:
                    topic_label = await _topic_title(chat_id, topic_id)
                except Exception:  # noqa: BLE001
                    topic_label = None

                line += f" | topic_id={topic_id}"
                if topic_label:
                    line += f" | {topic_label}"
                if ent is not None:
                    turl = _message_link(ent, chat_id, topic_id)
                    if turl:
                        line += f" | {turl}"
            elif d.get("topic_title"):
                line += f" | topic_title=\"{d.get('topic_title')}\""

            out.append(line)

    return "\n".join(out)


async def _normalize_routes_filter_args(event, args: str) -> str | None:
    tokens = shlex.split(args or "")
    if not tokens:
        return (args or "").strip()

    dest_ids: list[int] = []
    topic_top: int | None = None
    topic_title: str | None = None
    keep: list[str] = []

    for tok in tokens:
        if looks_like_message_link(tok):
            try:
                chat_id, _, top = await _resolve_message_link(tok)
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 错误: 无法解析链接: {tok} ({type(exc).__name__})")
                return None

            dest_ids.append(int(chat_id))

            if top is not None and topic_top is None:
                topic_top = int(top)

                if topic_top == 1:
                    topic_title = "General"
                else:
                    try:
                        ent = await client.get_entity(int(chat_id))
                        msg = await client.get_messages(ent, ids=int(topic_top))
                        action = getattr(msg, "action", None) if msg else None
                        title = getattr(action, "title", None) if action else None
                        if title:
                            topic_title = str(title)
                    except Exception:  # noqa: BLE001
                        topic_title = None

            continue

        keep.append(tok)

    if dest_ids:
        seen: set[int] = set()
        uniq: list[int] = []
        for d in dest_ids:
            if d in seen:
                continue
            seen.add(d)
            uniq.append(d)
        keep.append("dest=" + ",".join(str(x) for x in uniq))

    # Prefer filtering by resolved topic title (works for both topic_id and topic_title routes).
    if topic_title:
        keep.append("topic=" + shlex.quote(topic_title))
    elif topic_top is not None:
        keep.append(f"topic_id={int(topic_top)}")

    return " ".join([x for x in keep if str(x).strip()]).strip()


async def _cmd_list_routes(event, args: str = ""):
    cfg = config_manager.load(force=True)
    relay = cfg.get("relay", {}) or {}
    routes = relay.get("routes", []) or []
    if not routes:
        await event.reply("🤖 routes 为空")
        return

    args2 = await _normalize_routes_filter_args(event, args)
    if args2 is None:
        return

    text = await _build_routes_report(list(routes), args2)
    if not text:
        await event.reply(
            "🤖 未匹配到 routes。用法: /list_routes [source=..] [dest=..] [topic=..] [topic_id=..] [free_text..]\n"
            "提示: 也可以直接传一个 Telegram 消息链接（t.me/...）来按 destination/topic 过滤。"
        )
        return

    if len(text) <= 3500:
        await event.reply(text)
        return

    tmp = tempfile.TemporaryDirectory(prefix="routes_")
    try:
        path = f"{tmp.name}/routes.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        await client.send_file(event.chat_id, path, caption="🤖 Routes 太长，已导出为文件")
    finally:
        tmp.cleanup()


async def _cmd_export_routes(event, args: str = ""):
    cfg = config_manager.load(force=True)
    relay = cfg.get("relay", {}) or {}
    routes = relay.get("routes", []) or []
    if not routes:
        await event.reply("🤖 routes 为空")
        return

    args2 = await _normalize_routes_filter_args(event, args)
    if args2 is None:
        return

    text = await _build_routes_report(list(routes), args2)
    if not text:
        await event.reply("🤖 未匹配到 routes。")
        return

    tmp = tempfile.TemporaryDirectory(prefix="routes_")
    try:
        path = f"{tmp.name}/routes.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        await client.send_file(event.chat_id, path, caption="🤖 Routes 导出")
    finally:
        tmp.cleanup()


def _save_routes(routes: list[dict]) -> None:
    cfg = config_manager.load(force=True)
    cfg.setdefault("relay", {})
    cfg["relay"]["routes"] = routes
    config_manager.save(cfg)


async def _cmd_add_route(event, args: str):
    tokens = shlex.split(args or "")
    if len(tokens) < 2:
        await event.reply(
            "🤖 用法: /add_route <source_chat_id[,source_chat_id...]> [source_topic=<top_msg_id>] <dest_chat>[@<topic_top_msg_id>] | <dest_chat>=\"<topic_title>\" ...\n"
            "或: /add_route <source_message_link> <dest_message_link> [dest_message_link...]"
        )
        return

    source_token = tokens[0]

    if looks_like_message_link(source_token):
        src_chat_id, _, src_topic = await _resolve_message_link(source_token)

        destinations: list[dict] = []
        non_link_dest_tokens: list[str] = []
        for t in tokens[1:]:
            if looks_like_message_link(t):
                chat_id, _, topic_top = await _resolve_message_link(t)
                dest: dict = {"chat_id": int(chat_id)}
                if topic_top is not None:
                    dest["topic_id"] = int(topic_top)
                destinations.append(dest)
            else:
                non_link_dest_tokens.append(t)

        destinations.extend(_parse_destinations(non_link_dest_tokens))

        if not destinations:
            await event.reply("🤖 错误: destinations 为空")
            return

        cfg = config_manager.load(force=True)
        cfg.setdefault("relay", {})
        routes = list((cfg.get("relay", {}) or {}).get("routes", []) or [])

        new_route: dict = {"source_chats": [int(src_chat_id)], "destinations": destinations}
        if src_topic is not None:
            new_route["source_topics"] = [int(src_topic)]

        routes.append(new_route)
        _save_routes(routes)
        await event.reply("🤖 已添加 route")
        return

    source_chats = [int(x) for x in tokens[0].split(",") if x.strip()]
    source_topic_id = None

    dest_tokens: list[str] = []
    for t in tokens[1:]:
        if t.startswith("source_topic=") or t.startswith("topic="):
            source_topic_id = int(t.split("=", 1)[1])
            continue
        dest_tokens.append(t)

    destinations: list[dict] = []
    non_link_dest_tokens: list[str] = []
    for t in dest_tokens:
        if looks_like_message_link(t):
            chat_id, _, topic_top = await _resolve_message_link(t)
            dest: dict = {"chat_id": int(chat_id)}
            if topic_top is not None:
                dest["topic_id"] = int(topic_top)
            destinations.append(dest)
        else:
            non_link_dest_tokens.append(t)

    destinations.extend(_parse_destinations(non_link_dest_tokens))
    if not destinations:
        await event.reply("🤖 错误: destinations 为空")
        return

    cfg = config_manager.load(force=True)
    cfg.setdefault("relay", {})
    routes = list((cfg.get("relay", {}) or {}).get("routes", []) or [])

    new_route: dict = {"source_chats": source_chats, "destinations": destinations}
    if source_topic_id is not None:
        new_route["source_topics"] = [int(source_topic_id)]

    routes.append(new_route)
    _save_routes(routes)
    await event.reply("🤖 已添加 route")


async def _cmd_remove_route(event, args: str):
    tokens = shlex.split(args or "")
    if len(tokens) != 1:
        await event.reply("🤖 用法: /remove_route <route_index>")
        return

    idx = int(tokens[0]) - 1
    cfg = config_manager.load(force=True)
    relay = cfg.get("relay", {}) or {}
    routes = list(relay.get("routes", []) or [])

    if idx < 0 or idx >= len(routes):
        await event.reply("🤖 错误: route_index 超出范围")
        return

    routes.pop(idx)
    _save_routes(routes)
    await event.reply("🤖 已移除 route")


async def _cmd_set_destinations(event, args: str):
    tokens = shlex.split(args or "")
    if len(tokens) < 2:
        await event.reply(
            "🤖 用法: /set_destinations <route_index> <dest_chat>[@<topic_top_msg_id>] | <dest_chat>=\"<topic_title>\" ...\n"
            "或: /set_destinations <route_index> <dest_message_link> [dest_message_link...]\n"
            "示例: /set_destinations 3 -1002222222222=\"🔥 Hot Right Now\" -1003333333333=\"🔥 今日爆熱\""
        )
        return

    idx = int(tokens[0]) - 1

    destinations: list[dict] = []
    non_link_tokens: list[str] = []
    for t in tokens[1:]:
        if looks_like_message_link(t):
            chat_id, _, topic_top = await _resolve_message_link(t)
            dest: dict = {"chat_id": int(chat_id)}
            if topic_top is not None:
                dest["topic_id"] = int(topic_top)
            destinations.append(dest)
        else:
            non_link_tokens.append(t)

    destinations.extend(_parse_destinations(non_link_tokens))

    cfg = config_manager.load(force=True)
    relay = cfg.get("relay", {}) or {}
    routes = list(relay.get("routes", []) or [])

    if idx < 0 or idx >= len(routes):
        await event.reply("🤖 错误: route_index 超出范围")
        return

    routes[idx]["destinations"] = destinations
    _save_routes(routes)
    await event.reply("🤖 已更新 destinations")


async def _cmd_list_topics(event, args: str):
    tokens = shlex.split(args or "")
    if not tokens:
        await event.reply("🤖 用法: /list_topics <chat_id_or_username> [limit]\n示例: /list_topics -1001234567890 50")
        return

    chat_ref = tokens[0]
    limit = int(tokens[1]) if len(tokens) > 1 else 50
    limit = max(1, min(limit, 200))

    try:
        ent = await client.get_entity(chat_ref)
        peer = await client.get_input_entity(ent)

        offset_date = 0
        offset_id = 0
        offset_topic = 0
        remaining = limit
        topics = []

        while remaining > 0:
            batch = min(100, remaining)
            res = await client(
                functions.messages.GetForumTopicsRequest(
                    peer=peer,
                    q="",
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=batch,
                )
            )

            if not getattr(res, "topics", None):
                break

            for t in res.topics:
                topics.append(t)
            last = res.topics[-1]
            offset_date = int(getattr(last, "date", 0) or 0)
            offset_id = int(getattr(last, "top_message", 0) or 0)
            offset_topic = int(getattr(last, "id", 0) or 0)
            remaining = limit - len(topics)

            if len(res.topics) < batch:
                break

        if not topics:
            await event.reply("🤖 未找到 topics（可能该群未启用话题，或无权限）")
            return

        lines = []
        for t in topics[:limit]:
            title = str(getattr(t, "title", ""))
            top_message = int(getattr(t, "top_message", 0) or 0)
            topic_id = int(getattr(t, "id", 0) or 0)
            lines.append(f"{title} | top_message={top_message} | topic_id={topic_id}")

        await event.reply("🤖 Topics (use top_message for source_topic):\n" + "\n".join(lines[:80]))
    except Exception as exc:  # noqa: BLE001
        await event.reply(f"🤖 读取 topics 失败: {type(exc).__name__}: {exc}")


async def _cmd_help(event):
    text = "\n".join(
        [
            "🤖 Userbot help (DM commands to this userbot; recommended: Saved Messages)",
            "",
            "Tip: For easiest route management, DM your relay bot and send /start to use the interactive menu.",
            "",
            "Listen management:",
            "- /join <chat_link_or_username>",
            "  Examples:",
            "    /join https://t.me/somechannel",
            "    /join @somegroup",
            "- /leave <chat_id_or_username>",
            "  Examples:",
            "    /leave -1001234567890",
            "    /leave @somechannel",
            "- /add_listen <source_chat_id> <@relay_bot_username>",
            "  Examples:",
            "    /add_listen -1001234567890 @YourRelayBot",
            "    /add_listen -1009998887776 @YourRelayBot",
            "- /remove_listen <source_chat_id>",
            "  Example: /remove_listen -1001234567890",
            "- /list_listen",
            "",
            "Routes (edits relay.routes in config.json):",
            "- /add_route <source_chat[,source_chat...]> [source_topic=<top_msg_id>] <dest...>",
            "  Dest formats:",
            "    <dest_chat_id>=\"<topic_title>\"",
            "    <dest_chat_id>@<topic_top_message_id>",
            "    <dest_chat_id>",
            "  Examples:",
            "    /add_route -1001234567890 -1002222222222=\"🔥 Hot Right Now\" -1003333333333=\"🔥 今日爆熱\"",
            "    /add_route -1001234567890 source_topic=777 -1002222222222=\"Topic A\"",
            "  Link-based route form:",
            "    /add_route <source_message_link> <dest_message_link> [dest_message_link...]",
            "  Example:",
            "    /add_route https://t.me/c/111/222/333 https://t.me/c/444/555/666",
            "- /remove_route <index>  (index is the number shown in /list_routes)",
            "  Example: /remove_route 3",
            "- /set_destinations <index> <dest...>",
            "  Example: /set_destinations 3 -1002222222222=\"🔥 Hot Right Now\"",
            "",
            "Routes viewing / filtering:",
            "- /list_routes [filters...]",
            "  Filters: source=<id[,..]> dest=<id[,..]> topic=<substring> topic_id=<id> + free text terms",
            "  Examples:",
            "    /list_routes",
            "    /list_routes source=-1001234567890",
            "    /list_routes source=-1001234567890,-1009998887776",
            "    /list_routes dest=-1002222222222",
            "    /list_routes topic=\"Hot\"",
            "- /export_routes [filters...]  (always sends a routes.txt file)",
            "  Examples:",
            "    /export_routes",
            "    /export_routes source=-1001234567890",
            "",
            "Forum utilities:",
            "- /list_topics <chat_id_or_username> [limit]",
            "  Examples:",
            "    /list_topics -1001234567890",
            "    /list_topics @someforumgroup 100",
            "",
            "Twitter/X watch:",
            "- /add_x_watch (old) <x_profile_or_username> <source_chat_id> <@relay_bot> [poll_interval_sec=300] [fetch_limit=30] [archive_file=state/... ]",
            "  Example:",
            "    /add_x_watch https://x.com/Mastertpe1125 -900000001 @YourRelayBot poll_interval_sec=3600 fetch_limit=30",
            "- /add_x_watch (new) <x_profile_or_username> <dest_message_link> [dest_message_link...] [poll_interval_sec=...] [fetch_limit=...] [archive_file=...]",
            "  Notes:",
            "    - new form auto allocates source_chat_id and auto adds/updates a route.",
            "    - relay_bot can be provided via relay_bot=@YourRelayBot; otherwise we try to infer it from bot_mappings.",
            "  Example:",
            "    /add_x_watch https://x.com/Mastertpe1125 https://t.me/coolgayvid/80369 poll_interval_sec=3600 fetch_limit=30",
            "- /list_x_watch",
            "- /remove_x_watch <index>",
            "",
            "Other:",
            "- /help or /start",
        ]
    )

    if len(text) <= 3500:
        await event.reply(text)
        return

    tmp = tempfile.TemporaryDirectory(prefix="help_")
    try:
        path = f"{tmp.name}/help.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        await client.send_file(event.chat_id, path, caption="🤖 Help")
    finally:
        tmp.cleanup()


async def main():
    await client.start()
    log_event(logger, logging.INFO, "userbot_started")
    await rebuild_forwarding_map()
    asyncio.create_task(twitter_watch_loop())

    @client.on(NewMessage(func=lambda e: e.is_private and e.sender_id == settings["master_account_id"]))
    async def command_handler(event):
        raw = str(getattr(event.message, "text", "") or "")
        cmd, args = parse_command(raw)
        if not cmd:
            t = raw.strip()
            head = str(t.split(" ", 1)[0] or "").casefold().strip()
            if head in {
                "help",
                "start",
                "join",
                "leave",
                "add_listen",
                "remove_listen",
                "list_listen",
                "list_routes",
                "export_routes",
                "add_route",
                "remove_route",
                "set_destinations",
                "list_topics",
                "add_x_watch",
                "list_x_watch",
                "remove_x_watch",
            }:
                cmd, args = parse_command("/" + t)
            else:
                return

        if cmd == "/join":
            try:
                ent = await client.get_entity(args)
                await join_chat(ent)
                await event.reply(f"🤖 已尝试加入: {ent.title}")
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 加入失败: {type(exc).__name__}: {exc}")

        elif cmd == "/leave":
            try:
                ent = await client.get_entity(args)
                await leave_chat(ent)
                await event.reply(f"🤖 已尝试退出: {ent.title}")
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 退出失败: {type(exc).__name__}: {exc}")

        elif cmd == "/add_listen":
            sub_parts = (args or "").split(" ", 1)
            if len(sub_parts) != 2:
                await event.reply("🤖 用法: /add_listen <源ID> <@目标机器人>")
                return

            src, bot = sub_parts[0], sub_parts[1].strip()
            if not bot.startswith("@"):  
                await event.reply("🤖 错误: 机器人用户名需以 @ 开头")
                return

            try:
                ent = await client.get_entity(bot)
                if not getattr(ent, "bot", False):
                    await event.reply("🤖 错误: 目标必须是机器人账号（Bot），不能是频道/群/普通用户")
                    return

                exists = next((m for m in bot_mappings if str(m["source_chat"]) == str(src)), None)
                if exists:
                    if exists["target_bot"] == bot:
                        await event.reply(f"🤖 '{src}' 已经在监听列表中了。")
                    else:
                        new_map = [m for m in bot_mappings if str(m["source_chat"]) != str(src)]
                        new_map.append({"source_chat": src, "target_bot": bot})
                        update_config_file(new_map)
                        await event.reply(f"🤖 更新成功: {src} -> {bot}")
                else:
                    new_map = bot_mappings + [{"source_chat": src, "target_bot": bot}]
                    update_config_file(new_map)
                    await event.reply(f"🤖 添加成功: {src} -> {bot}")
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 操作失败: {type(exc).__name__}: {exc}")

        elif cmd == "/remove_listen":
            if not args:
                await event.reply("🤖 用法: /remove_listen <源ID>")
                return
            new_map = [m for m in bot_mappings if str(m["source_chat"]) != str(args)]
            if len(new_map) < len(bot_mappings):
                update_config_file(new_map)
                await event.reply(f"🤖 已移除监听: {args}")
            else:
                await event.reply(f"🤖 '{args}' 不在列表中。")

        elif cmd == "/list_listen":
            if bot_mappings:
                info = "\n".join([f"{m['source_chat']} -> {m['target_bot']}" for m in bot_mappings])
                await event.reply(f"🤖 当前监听:\n{info}")
            else:
                await event.reply("🤖 当前列表为空。")

        elif cmd == "/list_routes":
            await _cmd_list_routes(event, args)

        elif cmd == "/export_routes":
            await _cmd_export_routes(event, args)

        elif cmd == "/add_route":
            await _cmd_add_route(event, args)

        elif cmd == "/remove_route":
            await _cmd_remove_route(event, args)

        elif cmd == "/set_destinations":
            await _cmd_set_destinations(event, args)

        elif cmd == "/list_topics":
            await _cmd_list_topics(event, args)

        elif cmd == "/list_x_watch":
            cfg = config_manager.load(force=True)
            tw = cfg.get("twitter_watch", {}) or {}
            enabled = bool(tw.get("enabled", False))
            sources = tw.get("sources", []) or []

            if not sources:
                await event.reply(f"🤖 twitter_watch: enabled={enabled} | sources=0")
                return

            lines = [f"🤖 twitter_watch: enabled={enabled}", ""]
            for i, s in enumerate(sources, start=1):
                if not isinstance(s, dict):
                    continue
                profile = str(s.get("profile") or s.get("account") or s.get("url") or "")
                source_chat_id = s.get("source_chat_id")
                target_bot = s.get("target_bot")
                interval = s.get("poll_interval_sec", 300)
                fetch_limit = s.get("fetch_limit", 30)
                lines.append(
                    f"{i}) {profile} | source_chat_id={source_chat_id} | target_bot={target_bot} | interval={interval}s | fetch_limit={fetch_limit}"
                )

            await event.reply("\n".join(lines)[:3500])

        elif cmd == "/add_x_watch":
            tokens = shlex.split(args or "")
            if not tokens:
                await event.reply(
                    "🤖 用法(旧): /add_x_watch <x_profile_or_username> <source_chat_id> <@relay_bot> [poll_interval_sec=300] [fetch_limit=30] [archive_file=state/... ]\n"
                    "🤖 用法(新): /add_x_watch <x_profile_or_username> <dest_message_link> [dest_message_link...] [poll_interval_sec=...] [fetch_limit=...] [archive_file=...]\n"
                    "提示: 新用法会自动生成 source_chat_id，并自动添加 route。"
                )
                return

            def _is_int_token(s: str) -> bool:
                try:
                    int(str(s).strip())
                    return True
                except Exception:  # noqa: BLE001
                    return False

            def _infer_default_relay_bot(cfg: dict) -> str | None:
                bots = {str(m.get("target_bot") or "").strip() for m in (cfg.get("bot_mappings") or []) if isinstance(m, dict)}
                bots = {b for b in bots if b}
                if len(bots) == 1:
                    b = next(iter(bots))
                    if not b.startswith("@"):  # pragma: no cover
                        b = "@" + b
                    return b
                return None

            def _allocate_source_chat_id(sources: list[object]) -> int:
                used: set[int] = set()
                for s in sources:
                    if not isinstance(s, dict):
                        continue
                    try:
                        used.add(int(s.get("source_chat_id")))
                    except Exception:  # noqa: BLE001
                        continue
                # Pick a stable negative range.
                cand = -900000001
                while cand in used:
                    cand -= 1
                return cand

            def _dedupe_destinations(dests: list[dict]) -> list[dict]:
                seen: set[tuple[int, int | None, str | None]] = set()
                out: list[dict] = []
                for d in dests or []:
                    try:
                        chat_id = int(d.get("chat_id"))
                    except Exception:  # noqa: BLE001
                        continue
                    topic_id = int(d.get("topic_id")) if d.get("topic_id") is not None else None
                    topic_title = str(d.get("topic_title")) if d.get("topic_title") else None
                    key = (chat_id, topic_id, topic_title)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(d)
                return out

            profile = tokens[0]

            positional: list[str] = []
            kv: dict[str, str] = {}
            for t in tokens[1:]:
                if "=" in t:
                    k, v = t.split("=", 1)
                    kv[k.strip()] = v.strip()
                else:
                    positional.append(t)

            old_style = False
            if len(positional) >= 2 and _is_int_token(positional[0]):
                # Old style: <profile> <source_chat_id> <@relay_bot> ...
                old_style = True

            cfg = config_manager.load(force=True)
            tw = cfg.get("twitter_watch", {}) or {}
            sources = list(tw.get("sources", []) or [])

            destinations: list[dict] = []
            non_link_dest_tokens: list[str] = []

            if old_style:
                source_chat_id = int(positional[0])
                bot = "@" + positional[1].strip().lstrip("@")
                dest_tokens = positional[2:]

                for t in dest_tokens:
                    if looks_like_message_link(t):
                        try:
                            chat_id, _, topic_top = await _resolve_message_link(t)
                        except Exception as exc:  # noqa: BLE001
                            await event.reply(f"🤖 错误: 无法解析链接: {t} ({type(exc).__name__})")
                            return

                        dest: dict = {"chat_id": int(chat_id)}
                        if topic_top is not None:
                            dest["topic_id"] = int(topic_top)
                        destinations.append(dest)
                    else:
                        non_link_dest_tokens.append(t)
            else:
                # New style: <profile> <dest_link...> [poll_interval_sec=...] ...
                bot = kv.get("relay_bot") or kv.get("bot") or ""

                # Also allow passing the relay bot as a positional "@BotUserName" token.
                pos_bot = next((p for p in positional if str(p).strip().startswith("@")), None)
                if pos_bot:
                    positional = [p for p in positional if p != pos_bot]
                    bot = str(pos_bot)

                if bot:
                    bot = "@" + str(bot).strip().lstrip("@")
                else:
                    inferred = _infer_default_relay_bot(cfg)
                    if not inferred:
                        await event.reply(
                            "🤖 错误: 无法推断 relay bot 用户名。请用旧用法，或在新用法里加 relay_bot=@YourRelayBot。"
                        )
                        return
                    bot = inferred

                source_chat_id = int(kv.get("source_chat_id") or _allocate_source_chat_id(sources))

                for t in positional:
                    if looks_like_message_link(t):
                        try:
                            chat_id, _, topic_top = await _resolve_message_link(t)
                        except Exception as exc:  # noqa: BLE001
                            await event.reply(f"🤖 错误: 无法解析链接: {t} ({type(exc).__name__})")
                            return

                        dest: dict = {"chat_id": int(chat_id)}
                        if topic_top is not None:
                            dest["topic_id"] = int(topic_top)
                        destinations.append(dest)
                    else:
                        non_link_dest_tokens.append(t)

            if bot == "@":
                await event.reply("🤖 错误: 机器人用户名需以 @ 开头")
                return

            poll_interval_sec = int(kv.get("poll_interval_sec") or kv.get("interval") or 300)
            fetch_limit = int(kv.get("fetch_limit") or kv.get("limit") or 30)
            archive_file = kv.get("archive_file")

            try:
                ent = await client.get_entity(bot)
                if not getattr(ent, "bot", False):
                    await event.reply("🤖 错误: 目标必须是机器人账号（Bot），不能是频道/群/普通用户")
                    return
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 无法解析 bot: {type(exc).__name__}: {exc}")
                return

            destinations.extend(_parse_destinations(non_link_dest_tokens))
            destinations = _dedupe_destinations(destinations)

            new_entry: dict = {
                "profile": str(profile),
                "source_chat_id": int(source_chat_id),
                "target_bot": bot,
                "poll_interval_sec": max(30, int(poll_interval_sec)),
                "fetch_limit": max(1, min(int(fetch_limit), 200)),
            }
            if archive_file:
                new_entry["archive_file"] = str(archive_file)

            updated = False
            for i, s in enumerate(sources):
                if not isinstance(s, dict):
                    continue
                existing_profile = str(s.get("profile") or s.get("account") or s.get("url") or "")
                try:
                    existing_source = int(s.get("source_chat_id") or 0)
                except Exception:  # noqa: BLE001
                    existing_source = 0

                if existing_profile == str(profile) and existing_source == int(source_chat_id):
                    sources[i] = new_entry
                    updated = True
                    break

            if not updated:
                sources.append(new_entry)

            tw["enabled"] = True
            tw["sources"] = sources
            cfg["twitter_watch"] = tw

            # Optional: auto-add a route when using dest links.
            if destinations:
                cfg.setdefault("relay", {})
                routes = list((cfg.get("relay", {}) or {}).get("routes", []) or [])

                merged = False
                for r in routes:
                    if not isinstance(r, dict):
                        continue
                    srcs = [int(x) for x in (r.get("source_chats") or []) if str(x).strip()]
                    if srcs == [int(source_chat_id)] and not (r.get("source_topics") or []):
                        r["destinations"] = _dedupe_destinations(list(r.get("destinations") or []) + destinations)
                        merged = True
                        break

                if not merged:
                    routes.append({"source_chats": [int(source_chat_id)], "destinations": destinations})

                cfg["relay"]["routes"] = routes

            config_manager.save(cfg)

            if destinations:
                await event.reply(
                    "🤖 已添加 twitter_watch source，并已添加/更新 route。\n"
                    f"profile={profile} | source_chat_id={source_chat_id} | target_bot={bot} | interval={new_entry['poll_interval_sec']}s | fetch_limit={new_entry['fetch_limit']}\n"
                    f"destinations={_format_destinations(destinations)}"
                )
            else:
                await event.reply(
                    "🤖 已添加 twitter_watch source。\n"
                    "下一步: 用 /add_route <source_chat_id> <dest...> 把 source_chat_id 路由到目标话题/频道。\n"
                    f"profile={profile} | source_chat_id={source_chat_id} | target_bot={bot}"
                )

        elif cmd == "/remove_x_watch":
            tokens = shlex.split(args or "")
            if len(tokens) != 1:
                await event.reply("🤖 用法: /remove_x_watch <index>")
                return

            idx = int(tokens[0]) - 1
            cfg = config_manager.load(force=True)
            tw = cfg.get("twitter_watch", {}) or {}
            sources = list(tw.get("sources", []) or [])

            if idx < 0 or idx >= len(sources):
                await event.reply("🤖 错误: index 超出范围")
                return

            removed = sources.pop(idx)
            tw["sources"] = sources
            cfg["twitter_watch"] = tw
            config_manager.save(cfg)

            profile = None
            try:
                profile = str((removed or {}).get("profile") or "")
            except Exception:  # noqa: BLE001
                profile = None

            await event.reply(f"🤖 已移除 twitter_watch: {profile or '(unknown)'}")

        else:
            await _cmd_help(event)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
