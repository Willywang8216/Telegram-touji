import asyncio
import json
import logging
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.tl.functions.messages import CreateForumTopicRequest, GetForumTopicsRequest

from common_config import ConfigManager, load_relay_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from structured_logger import get_logger, log_event

logger = get_logger("relaybot")
config_manager = ConfigManager()
settings = load_relay_settings(config_manager)

media_group_cache = {}
media_group_lock = asyncio.Lock()
rate_limiter = AsyncRateLimiter(rate_per_sec=8)
DLQ_PATH = "logs/relay_dlq.jsonl"

client = TelegramClient("bot_session", settings["api_id"], settings["api_hash"]).start(bot_token=settings["bot_token"])

topic_cache: dict[tuple[int, str], int] = {}
_topic_locks: dict[tuple[int, str], asyncio.Lock] = {}
_topic_locks_guard = asyncio.Lock()


def _routing_fingerprint(cfg: dict[str, Any]) -> str:
    payload = {
        "dest_channels": cfg.get("dest_channels"),
        "default_dest_channels": cfg.get("default_dest_channels"),
        "routes_by_source": cfg.get("routes_by_source"),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


_settings_fp = _routing_fingerprint(settings)


def get_settings() -> dict[str, Any]:
    global settings, _settings_fp
    if config_manager.reload_if_changed():
        new_settings = load_relay_settings(config_manager)
        new_fp = _routing_fingerprint(new_settings)
        if new_fp != _settings_fp:
            topic_cache.clear()
            log_event(logger, logging.INFO, "topic_cache_cleared", reason="config_changed")
        settings = new_settings
        _settings_fp = new_fp
        log_event(
            logger,
            logging.INFO,
            "config_hot_reloaded",
            dest_channels=settings.get("dest_channels"),
            default_dest_channels=settings.get("default_dest_channels"),
            routes_by_source_count=len(settings.get("routes_by_source") or {}),
        )
    return settings


def _extract_source_peer_id(msg) -> int | None:
    fwd = getattr(msg, "fwd_from", None)
    from_id = getattr(fwd, "from_id", None) if fwd else None
    if not from_id:
        return None
    try:
        return utils.get_peer_id(from_id)
    except Exception:  # noqa: BLE001
        return None


def _normalize_destinations(raw_dests: list[Any]) -> list[dict[str, Any]]:
    """Normalize a destination list.

    Supported formats:
    - int: destination chat/channel id
    - {"dest_chat": int, "topic": str | None}: send to a forum topic (topic optional)
    """

    normalized: list[dict[str, Any]] = []
    for item in raw_dests:
        if isinstance(item, dict):
            dest_chat = item.get("dest_chat")
            if dest_chat is None:
                dest_chat = item.get("dest_channel")
            if dest_chat is None:
                dest_chat = item.get("dest")

            if dest_chat is None:
                log_event(logger, logging.ERROR, "invalid_route_destination", raw=item)
                continue

            topic = item.get("topic")
            if topic is None:
                topic = item.get("topic_title")
            # Treat empty string as no topic.
            topic_title = str(topic).strip() if topic is not None else None
            if topic_title == "":
                topic_title = None

            normalized.append({"dest_chat": int(dest_chat), "topic": topic_title})
        else:
            try:
                normalized.append({"dest_chat": int(item), "topic": None})
            except (TypeError, ValueError):
                log_event(logger, logging.ERROR, "invalid_destination", raw=item)
                continue
    return normalized


def _coerce_dest_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _select_destinations(source_peer_id: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = get_settings()
    routes_by_source = cfg.get("routes_by_source") or {}

    if not routes_by_source:
        raw_dests = cfg.get("dest_channels")
        meta = {"mode": "broadcast", "matched": False}
    elif source_peer_id is not None and source_peer_id in routes_by_source:
        raw_dests = routes_by_source[source_peer_id]
        meta = {"mode": "by_source", "matched": True}
    else:
        raw_dests = cfg.get("default_dest_channels") or cfg.get("dest_channels")
        meta = {"mode": "default", "matched": False}

    destinations = _normalize_destinations(_coerce_dest_list(raw_dests))
    return destinations, meta


async def _get_topic_lock(key: tuple[int, str]) -> asyncio.Lock:
    async with _topic_locks_guard:
        lock = _topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _topic_locks[key] = lock
        return lock


async def _find_forum_topic_top_message(dest_chat: int, topic_title: str) -> int | None:
    await rate_limiter.wait()
    result = await with_retry(
        lambda: client(
            GetForumTopicsRequest(
                peer=dest_chat,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100,
                q=topic_title,
            )
        ),
        retries=3,
        base_delay=1,
        logger=logger,
        action="get_forum_topics",
    )

    for t in getattr(result, "topics", []) or []:
        if getattr(t, "title", None) == topic_title:
            top = getattr(t, "top_message", None)
            if top:
                return int(top)
    return None


async def ensure_forum_topic(dest_chat: int, topic_title: str) -> int:
    key = (dest_chat, topic_title)
    cached = topic_cache.get(key)
    if cached:
        return cached

    lock = await _get_topic_lock(key)
    async with lock:
        cached = topic_cache.get(key)
        if cached:
            return cached

        top_message = await _find_forum_topic_top_message(dest_chat, topic_title)
        if top_message is None:
            await rate_limiter.wait()
            await with_retry(
                lambda: client(CreateForumTopicRequest(peer=dest_chat, title=topic_title)),
                retries=3,
                base_delay=1,
                logger=logger,
                action="create_forum_topic",
            )
            top_message = await _find_forum_topic_top_message(dest_chat, topic_title)

        if top_message is None:
            raise RuntimeError(f"Forum topic not found after create: chat={dest_chat} title={topic_title}")

        topic_cache[key] = top_message
        return top_message


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handler(event):
    sender = await event.get_sender()
    if sender and sender.is_self:
        return

    stripped_text = (event.raw_text or "").strip()
    if stripped_text.startswith("/"):
        log_event(logger, logging.INFO, "command_blocked", text=stripped_text)
        return
    if stripped_text.startswith("🤖"):
        log_event(logger, logging.INFO, "system_reply_blocked", text=stripped_text)
        return

    if event.message.grouped_id:
        async with media_group_lock:
            gid = event.message.grouped_id
            if gid not in media_group_cache:
                media_group_cache[gid] = {"messages": [], "task": None}
            media_group_cache[gid]["messages"].append(event.message)
            if media_group_cache[gid]["task"]:
                media_group_cache[gid]["task"].cancel()
            media_group_cache[gid]["task"] = asyncio.create_task(process_media_group(gid))
        log_event(logger, logging.INFO, "album_cached", group_id=event.message.grouped_id)
    else:
        await send_copy(event.message)


async def process_media_group(gid: int):
    try:
        await asyncio.sleep(2)
        async with media_group_lock:
            if gid not in media_group_cache:
                return
            msgs = media_group_cache[gid]["messages"]
            del media_group_cache[gid]

        msgs.sort(key=lambda x: x.id)
        media = [m.media for m in msgs]
        caption = next((m.text for m in msgs if m.text), None)

        source_peer_id = None
        for m in msgs:
            source_peer_id = _extract_source_peer_id(m)
            if source_peer_id is not None:
                break

        destinations, meta = _select_destinations(source_peer_id)
        log_event(
            logger,
            logging.INFO,
            "routing_decision",
            message_type="album",
            group_id=gid,
            source_peer_id=source_peer_id,
            **meta,
            destinations=destinations,
        )

        for dest in destinations:
            dest_chat = dest["dest_chat"]
            topic_title = dest.get("topic")

            try:
                reply_to = None
                if topic_title:
                    reply_to = await ensure_forum_topic(dest_chat, topic_title)

                await rate_limiter.wait()
                await with_retry(
                    lambda: client.send_message(dest_chat, message=caption, file=media, reply_to=reply_to),
                    retries=3,
                    base_delay=1,
                    logger=logger,
                    action="send_album",
                )
                log_event(
                    logger,
                    logging.INFO,
                    "album_sent",
                    channel_id=dest_chat,
                    group_id=gid,
                    topic=topic_title,
                    reply_to=reply_to,
                )
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "channel_id": dest_chat,
                    "group_id": gid,
                    "topic": topic_title,
                    "error": str(exc),
                }
                write_dlq(DLQ_PATH, payload)
                log_event(logger, logging.ERROR, "album_send_failed", **payload)
    except asyncio.CancelledError:
        return


async def send_copy(msg):
    source_peer_id = _extract_source_peer_id(msg)
    destinations, meta = _select_destinations(source_peer_id)
    log_event(
        logger,
        logging.INFO,
        "routing_decision",
        message_type="single",
        message_id=msg.id,
        source_peer_id=source_peer_id,
        **meta,
        destinations=destinations,
    )

    text = msg.message

    for dest in destinations:
        dest_chat = dest["dest_chat"]
        topic_title = dest.get("topic")

        try:
            reply_to = None
            if topic_title:
                reply_to = await ensure_forum_topic(dest_chat, topic_title)

            await rate_limiter.wait()
            await with_retry(
                lambda: client.send_message(dest_chat, message=text, file=msg.media, reply_to=reply_to),
                retries=3,
                base_delay=1,
                logger=logger,
                action="send_message",
            )
            log_event(
                logger,
                logging.INFO,
                "message_sent",
                channel_id=dest_chat,
                message_id=msg.id,
                topic=topic_title,
                reply_to=reply_to,
            )
        except Exception as exc:  # noqa: BLE001
            payload = {
                "channel_id": dest_chat,
                "message_id": msg.id,
                "topic": topic_title,
                "error": str(exc),
            }
            write_dlq(DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "message_send_failed", **payload)


log_event(
    logger,
    logging.INFO,
    "relaybot_started",
    dest_channels=settings.get("dest_channels"),
    default_dest_channels=settings.get("default_dest_channels"),
    routes_by_source_count=len(settings.get("routes_by_source") or {}),
)
client.run_until_disconnected()
