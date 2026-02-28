import asyncio
import logging
import random

from telethon import TelegramClient, events, functions, types, utils
from telethon.tl.types import PeerChannel

from common_config import ConfigManager, load_relay_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from message_filter import should_block_text
from structured_logger import get_logger, log_event
from telethon_spam import group_looks_like_promo_directory, message_looks_like_promo_directory

logger = get_logger("relaybot")
config_manager = ConfigManager()
settings = load_relay_settings(config_manager)

media_group_cache: dict[int, dict] = {}
media_group_lock = asyncio.Lock()
rate_limiter = AsyncRateLimiter(rate_per_sec=8)
DLQ_PATH = "logs/relay_dlq.jsonl"

client = TelegramClient("bot_session", settings["api_id"], settings["api_hash"]).start(bot_token=settings["bot_token"])

forum_topics_lock = asyncio.Lock()
forum_topics_cache: dict[int, dict[str, int]] = {}
topic_api_disabled_chats: set[int] = set()

BUILTIN_BLOCKLIST_SUBSTRINGS = [
    "Ban:  各类rush有货",
    "Contact the bot above if you would buy rush",
    "buy rush or purchase videos",

    # drug / med ads
    "rush",
    "poppers",
    "popper",
    "viagra",
    "cialis",
    "伟哥",
    "威而钢",
    "威而鋼",
    "春药",
    "春藥",
    "迷奸药",
    "迷奸藥",
]

SRC_MARKER_PREFIX = "[[SRC:"
SRC_MARKER_SUFFIX = "]]"


def current_settings() -> dict:
    global settings
    if config_manager.reload_if_changed():
        settings = load_relay_settings(config_manager)
        log_event(
            logger,
            logging.INFO,
            "config_hot_reloaded",
            default_destinations=settings.get("default_destinations"),
            route_count=len(settings.get("routes", [])),
        )
    return settings


def _normalize_destinations(destinations) -> list[dict]:
    if not destinations:
        return []
    normalized = []
    for d in destinations:
        if isinstance(d, int):
            normalized.append({"chat_id": d})
        else:
            normalized.append(d)
    return normalized


def _raw_message_text(msg) -> str:
    return (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()


def _extract_source_peer_id_from_marker(text: str) -> int | None:
    if not text:
        return None
    text = text.strip()
    if not text.startswith(SRC_MARKER_PREFIX):
        return None
    end = text.find(SRC_MARKER_SUFFIX)
    if end == -1:
        return None
    raw = text[len(SRC_MARKER_PREFIX) : end]
    try:
        return int(raw)
    except ValueError:
        return None


def _strip_source_marker(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    if not t.startswith(SRC_MARKER_PREFIX):
        return text
    end = t.find(SRC_MARKER_SUFFIX)
    if end == -1:
        return text
    return t[end + len(SRC_MARKER_SUFFIX) :].lstrip()


def _pick_post_caption(post_captions, chat_id: int) -> str:
    if not post_captions:
        return ""

    value = post_captions
    if isinstance(post_captions, dict):
        value = post_captions.get(str(chat_id))

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        items = [str(x) for x in value if x]
        if not items:
            return ""
        return random.choice(items)

    return ""


def _media_filter_kwargs(s: dict) -> dict:
    if bool(s.get("media_filter_use_general_blocklist", True)):
        return {
            "blocklist_substrings": BUILTIN_BLOCKLIST_SUBSTRINGS + list(s.get("blocklist_substrings") or []),
            "blocklist_regexes": s.get("blocklist_regexes") or [],
            "block_contact_ads": bool(s.get("block_contact_ads", True)),
            "contact_ad_keywords": s.get("contact_ad_keywords"),
        }

    return {
        "blocklist_substrings": BUILTIN_BLOCKLIST_SUBSTRINGS + list(s.get("media_blocklist_substrings") or []),
        "blocklist_regexes": s.get("media_blocklist_regexes") or [],
        "block_contact_ads": bool(s.get("media_block_contact_ads", False)),
        "contact_ad_keywords": s.get("media_contact_ad_keywords") or s.get("contact_ad_keywords"),
    }


def _extract_source_peer_id(msg) -> int | None:
    hdr = getattr(msg, "fwd_from", None)
    if not hdr:
        return _extract_source_peer_id_from_marker(_raw_message_text(msg))

    from_id = getattr(hdr, "from_id", None)
    if from_id:
        return utils.get_peer_id(from_id)

    channel_id = getattr(hdr, "channel_id", None)
    if channel_id:
        return utils.get_peer_id(PeerChannel(channel_id))

    saved_from_peer = getattr(hdr, "saved_from_peer", None)
    if saved_from_peer:
        return utils.get_peer_id(saved_from_peer)

    return None


async def _source_title(source_peer_id: int | None, msg) -> str | None:
    if not source_peer_id:
        return None

    hdr = getattr(msg, "fwd_from", None)
    from_name = getattr(hdr, "from_name", None) if hdr else None
    if from_name:
        return str(from_name)

    try:
        ent = await client.get_entity(source_peer_id)
        return getattr(ent, "title", None) or getattr(ent, "username", None)
    except Exception:  # noqa: BLE001
        return str(source_peer_id)


def _is_send_videos_forbidden(exc: Exception) -> bool:
    return "chat_send_videos_forbidden" in str(exc).lower()


def _topic_title_variants(title: str) -> list[str]:
    if not title:
        return []

    t = str(title)
    out = [t]

    # If config got updated to add a leading emoji ("🍑 Foo") but the mapping
    # still uses the old title ("Foo"), try a fallback.
    if " " in t:
        first, rest = t.split(" ", 1)
        if len(first) <= 8 and rest:
            out.append(rest)

    return out


def _preconfigured_topic_top_message(chat_id: int, title: str) -> int | None:
    s = current_settings()
    mapping = s.get("forum_topic_top_messages") or {}

    chat_map = None
    for key in (str(chat_id), chat_id):
        if key in mapping:
            chat_map = mapping.get(key)
            break

    if not isinstance(chat_map, dict):
        return None

    for variant in _topic_title_variants(title):
        value = chat_map.get(variant)
        if value is not None:
            return int(value)

    return None


def _fallback_topic_title(chat_id: int) -> str | None:
    s = current_settings()
    mapping = s.get("fallback_topic_titles") or {}
    if not isinstance(mapping, dict):
        return None

    for key in (str(chat_id), chat_id):
        if key in mapping:
            value = mapping.get(key)
            if value:
                return str(value)
            return None

    return None


async def _send_media_with_caption(chat_id: int, media, caption: str, reply_to: int | None, *, action: str) -> None:
    try:
        await with_retry(
            lambda: client.send_message(chat_id, message=caption, file=media, reply_to=reply_to),
            retries=3,
            base_delay=1,
            logger=logger,
            action=action,
        )
    except Exception as exc:  # noqa: BLE001
        if media is not None and _is_send_videos_forbidden(exc):
            await with_retry(
                lambda: client.send_file(chat_id, media, caption=caption, reply_to=reply_to, force_document=True),
                retries=3,
                base_delay=1,
                logger=logger,
                action=f"{action}_force_document",
            )
            return
        raise


async def _get_forum_topic_top_message(chat_id: int, title: str) -> int | None:
    if not title:
        return None

    pre = _preconfigured_topic_top_message(chat_id, title)
    if pre:
        return pre

    if chat_id in topic_api_disabled_chats:
        return None

    async with forum_topics_lock:
        cached = forum_topics_cache.get(chat_id, {}).get(title)
        if cached:
            return cached

        entity = await client.get_entity(chat_id)

        try:
            res = await client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                    q="",
                )
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "cannot be executed as a bot" in msg or "bot users is restricted" in msg:
                if chat_id not in topic_api_disabled_chats:
                    log_event(logger, logging.WARNING, "topic_api_disabled", chat_id=chat_id, error=str(exc))
                topic_api_disabled_chats.add(chat_id)
                return None
            raise

        forum_topics_cache[chat_id] = {t.title: t.top_message for t in res.topics}

        if title not in forum_topics_cache[chat_id]:
            try:
                await client(functions.messages.CreateForumTopicRequest(peer=entity, title=title, icon_color=0x6FB9F0))
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "cannot be executed as a bot" in msg or "bot users is restricted" in msg:
                    if chat_id not in topic_api_disabled_chats:
                        log_event(logger, logging.WARNING, "topic_api_disabled", chat_id=chat_id, error=str(exc))
                    topic_api_disabled_chats.add(chat_id)
                    return None
                raise

            res = await client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                    q="",
                )
            )
            forum_topics_cache[chat_id] = {t.title: t.top_message for t in res.topics}

        return forum_topics_cache[chat_id].get(title)


def _resolve_destinations_for_source(source_peer_id: int | None) -> list[dict]:
    s = current_settings()

    if source_peer_id is not None:
        for route in s.get("routes", []):
            try:
                if "source_chat" in route and int(route.get("source_chat")) == int(source_peer_id):
                    return _normalize_destinations(route.get("destinations") or route.get("dest_channels") or [])

                src_list = route.get("source_chats")
                if isinstance(src_list, list) and any(int(x) == int(source_peer_id) for x in src_list):
                    return _normalize_destinations(route.get("destinations") or route.get("dest_channels") or [])
            except Exception:  # noqa: BLE001
                continue

    return _normalize_destinations(s.get("default_destinations"))


def _bucket_topic_title(bucket_cfg: dict, source_peer_id: int | None, message_id: int) -> str | None:
    if not bucket_cfg:
        return None
    prefix = bucket_cfg.get("prefix")
    count = int(bucket_cfg.get("count") or 0)
    start = int(bucket_cfg.get("start") or 1)
    if not prefix or count <= 0:
        return None

    mode = str(bucket_cfg.get("by") or bucket_cfg.get("mode") or "source").lower().strip()
    if mode in {"message", "msg", "message_id", "msg_id"}:
        key = message_id
    else:
        key = source_peer_id if source_peer_id is not None else message_id

    idx = (abs(int(key)) % count) + start
    return f"{prefix}{idx}"


async def _send_to_destination(
    msg, destination: dict, source_peer_id: int | None = None, source_topic_title: str | None = None
) -> bool:
    chat_id = int(destination.get("chat_id"))

    topic_title = destination.get("topic_title") or destination.get("topic")
    if destination.get("topic_from_source"):
        topic_title = source_topic_title

    bucket_cfg = destination.get("bucket_topics") or destination.get("bucket")
    if bucket_cfg:
        topic_title = _bucket_topic_title(bucket_cfg, source_peer_id=source_peer_id, message_id=msg.id)

    s = current_settings()

    reply_to = None
    used_fallback_topic = False

    if topic_title:
        try:
            reply_to = await _get_forum_topic_top_message(chat_id, str(topic_title))
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "topic_lookup_failed", chat_id=chat_id, title=str(topic_title), error=str(exc))
            reply_to = None

        if reply_to is None:
            fallback_title = _fallback_topic_title(chat_id)
            if fallback_title and str(fallback_title) != str(topic_title):
                try:
                    reply_to = await _get_forum_topic_top_message(chat_id, str(fallback_title))
                    if reply_to is not None:
                        used_fallback_topic = True
                        log_event(
                            logger,
                            logging.INFO,
                            "topic_fallback_used",
                            chat_id=chat_id,
                            requested=str(topic_title),
                            fallback=str(fallback_title),
                        )
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        logger,
                        logging.ERROR,
                        "topic_fallback_lookup_failed",
                        chat_id=chat_id,
                        requested=str(topic_title),
                        fallback=str(fallback_title),
                        error=str(exc),
                    )

        # If we can't resolve the topic, default behaviour is: either send to General (reply_to=None)
        # or skip entirely. If a per-chat fallback topic is configured, prefer sending rather than
        # dropping the message.
        if reply_to is None and not bool(s.get("fallback_to_general_topic", True)) and _fallback_topic_title(chat_id) is None:
            log_event(logger, logging.WARNING, "topic_unresolved_skipped", chat_id=chat_id, title=str(topic_title))
            return False

    media = msg.media
    if isinstance(media, types.MessageMediaWebPage):
        media = None

    # Always drop text-only messages.
    if media is None:
        return False

    override_caption = _pick_post_caption(s.get("post_captions"), chat_id)
    if override_caption:
        caption = override_caption
    elif s.get("strip_text"):
        caption = ""
    else:
        caption = _strip_source_marker(_raw_message_text(msg))

    await rate_limiter.wait()
    try:
        await _send_media_with_caption(chat_id, media, caption, reply_to, action="send_message")
        return True
    except Exception as exc:  # noqa: BLE001
        fallback_title = _fallback_topic_title(chat_id)
        if fallback_title and not used_fallback_topic:
            try:
                fallback_reply_to = await _get_forum_topic_top_message(chat_id, str(fallback_title))
                await rate_limiter.wait()
                await _send_media_with_caption(chat_id, media, caption, fallback_reply_to, action="send_message_fallback")
                log_event(logger, logging.INFO, "send_fallback_succeeded", chat_id=chat_id, fallback=str(fallback_title))
                return True
            except Exception as fallback_exc:  # noqa: BLE001
                log_event(
                    logger,
                    logging.ERROR,
                    "send_fallback_failed",
                    chat_id=chat_id,
                    requested=str(topic_title) if topic_title else None,
                    fallback=str(fallback_title),
                    error=str(fallback_exc),
                )

        raise


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handler(event):
    sender = await event.get_sender()
    if sender and sender.is_self:
        return

    s = current_settings()
    allowed = set(int(x) for x in (s.get("allowed_sender_ids") or []))
    if allowed and int(event.sender_id) not in allowed:
        # Private relay: ignore DMs from non-whitelisted users.
        log_event(logger, logging.WARNING, "unauthorized_sender", sender_id=int(event.sender_id))
        return

    stripped_text = (event.raw_text or "").strip()
    if stripped_text.startswith("/"):
        # Keep relaybot command surface disabled by default.
        # If you later add an admin UI, gate it by sender_id here.
        log_event(logger, logging.INFO, "command_blocked", text=stripped_text)
        return
    if stripped_text.startswith("🤖"):
        log_event(logger, logging.INFO, "system_reply_blocked", text=stripped_text)
        return

    msg = event.message
    media = msg.media
    if isinstance(media, types.MessageMediaWebPage):
        media = None

    if media is None and not msg.grouped_id:
        return

    s = current_settings()

    filter_text = _strip_source_marker(_raw_message_text(msg))
    if should_block_text(filter_text, **_media_filter_kwargs(s)):
        log_event(logger, logging.INFO, "message_blocked", reason="blocklist", message_id=msg.id)
        return

    if message_looks_like_promo_directory(msg):
        log_event(logger, logging.INFO, "message_blocked", reason="promo_directory", message_id=msg.id)
        return

    if msg.grouped_id:
        async with media_group_lock:
            gid = msg.grouped_id
            if gid not in media_group_cache:
                media_group_cache[gid] = {"messages": [], "task": None}
            media_group_cache[gid]["messages"].append(msg)
            if media_group_cache[gid]["task"]:
                media_group_cache[gid]["task"].cancel()
            media_group_cache[gid]["task"] = asyncio.create_task(process_media_group(gid))
        log_event(logger, logging.INFO, "album_cached", group_id=msg.grouped_id)
    else:
        await send_copy(msg)


async def process_media_group(gid: int):
    try:
        await asyncio.sleep(2)
        async with media_group_lock:
            if gid not in media_group_cache:
                return
            msgs = media_group_cache[gid]["messages"]
            del media_group_cache[gid]

        msgs.sort(key=lambda x: x.id)
        media = [m.media for m in msgs if m.media is not None and not isinstance(m.media, types.MessageMediaWebPage)]
        original_caption = next((_raw_message_text(m) for m in msgs if _raw_message_text(m)), "")

        s = current_settings()

        album_text = "\n".join([_strip_source_marker(_raw_message_text(m)) for m in msgs if _raw_message_text(m)])
        if should_block_text(album_text, **_media_filter_kwargs(s)):
            log_event(logger, logging.INFO, "album_blocked", reason="blocklist", group_id=gid)
            return

        if group_looks_like_promo_directory(msgs):
            log_event(logger, logging.INFO, "album_blocked", reason="promo_directory", group_id=gid)
            return

        if not media:
            return

        source_peer_id = None
        for m in msgs:
            source_peer_id = _extract_source_peer_id(m)
            if source_peer_id is not None:
                break

        source_title = await _source_title(source_peer_id, msgs[0])
        destinations = _resolve_destinations_for_source(source_peer_id)

        for dest in destinations:
            chat_id = int(dest.get("chat_id"))
            topic_title = dest.get("topic_title") or dest.get("topic")
            if dest.get("topic_from_source"):
                topic_title = source_title

            bucket_cfg = dest.get("bucket_topics") or dest.get("bucket")
            if bucket_cfg:
                topic_title = _bucket_topic_title(bucket_cfg, source_peer_id=source_peer_id, message_id=msgs[0].id)

            reply_to = None
            used_fallback_topic = False

            if topic_title:
                try:
                    reply_to = await _get_forum_topic_top_message(chat_id, str(topic_title))
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        logger,
                        logging.ERROR,
                        "topic_lookup_failed",
                        chat_id=chat_id,
                        title=str(topic_title),
                        error=str(exc),
                    )
                    reply_to = None

                if reply_to is None:
                    fallback_title = _fallback_topic_title(chat_id)
                    if fallback_title and str(fallback_title) != str(topic_title):
                        try:
                            reply_to = await _get_forum_topic_top_message(chat_id, str(fallback_title))
                            if reply_to is not None:
                                used_fallback_topic = True
                                log_event(
                                    logger,
                                    logging.INFO,
                                    "topic_fallback_used",
                                    chat_id=chat_id,
                                    requested=str(topic_title),
                                    fallback=str(fallback_title),
                                )
                        except Exception as exc:  # noqa: BLE001
                            log_event(
                                logger,
                                logging.ERROR,
                                "topic_fallback_lookup_failed",
                                chat_id=chat_id,
                                requested=str(topic_title),
                                fallback=str(fallback_title),
                                error=str(exc),
                            )

                if reply_to is None and not bool(s.get("fallback_to_general_topic", True)) and _fallback_topic_title(chat_id) is None:
                    log_event(logger, logging.WARNING, "topic_unresolved_skipped", chat_id=chat_id, title=str(topic_title))
                    continue

            override_caption = _pick_post_caption(s.get("post_captions"), chat_id)
            if override_caption:
                caption = override_caption
            elif s.get("strip_text"):
                caption = ""
            else:
                caption = _strip_source_marker(original_caption or "")

            await rate_limiter.wait()
            try:
                await _send_media_with_caption(chat_id, media, caption, reply_to, action="send_album")
                log_event(logger, logging.INFO, "album_sent", chat_id=chat_id, group_id=gid)
            except Exception as exc:  # noqa: BLE001
                fallback_title = _fallback_topic_title(chat_id)
                if fallback_title and not used_fallback_topic:
                    try:
                        fallback_reply_to = await _get_forum_topic_top_message(chat_id, str(fallback_title))
                        await rate_limiter.wait()
                        await _send_media_with_caption(chat_id, media, caption, fallback_reply_to, action="send_album_fallback")
                        log_event(logger, logging.INFO, "send_fallback_succeeded", chat_id=chat_id, fallback=str(fallback_title))
                        continue
                    except Exception as fallback_exc:  # noqa: BLE001
                        log_event(
                            logger,
                            logging.ERROR,
                            "send_fallback_failed",
                            chat_id=chat_id,
                            requested=str(topic_title) if topic_title else None,
                            fallback=str(fallback_title),
                            error=str(fallback_exc),
                        )

                payload = {"chat_id": chat_id, "group_id": gid, "error": str(exc)}
                write_dlq(DLQ_PATH, payload)
                log_event(logger, logging.ERROR, "album_send_failed", **payload)
    except asyncio.CancelledError:
        return


async def send_copy(msg):
    source_peer_id = _extract_source_peer_id(msg)
    source_title = await _source_title(source_peer_id, msg)
    destinations = _resolve_destinations_for_source(source_peer_id)

    for dest in destinations:
        try:
            sent = await _send_to_destination(msg, dest, source_peer_id=source_peer_id, source_topic_title=source_title)
            if sent:
                log_event(logger, logging.INFO, "message_sent", chat_id=int(dest.get("chat_id")), message_id=msg.id)
        except Exception as exc:  # noqa: BLE001
            payload = {"chat_id": int(dest.get("chat_id")), "message_id": msg.id, "error": str(exc)}
            write_dlq(DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "message_send_failed", **payload)


async def ensure_forum_topics_on_startup():
    s = current_settings()
    for item in s.get("ensure_forum_topics", []) or []:
        chat_id = int(item.get("chat_id"))
        for title in item.get("topics", []) or []:
            try:
                top = await _get_forum_topic_top_message(chat_id, str(title))
                if top:
                    log_event(logger, logging.INFO, "topic_ensured", chat_id=chat_id, title=str(title), top_message=int(top))
                else:
                    log_event(logger, logging.WARNING, "topic_ensure_skipped", chat_id=chat_id, title=str(title), reason="unavailable")
            except Exception as exc:  # noqa: BLE001
                log_event(logger, logging.ERROR, "topic_ensure_failed", chat_id=chat_id, title=str(title), error=str(exc))


log_event(
    logger,
    logging.INFO,
    "relaybot_started",
    default_destinations=settings.get("default_destinations"),
    route_count=len(settings.get("routes", [])),
)
client.loop.create_task(ensure_forum_topics_on_startup())
client.run_until_disconnected()
