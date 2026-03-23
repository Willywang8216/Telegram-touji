import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, functions

# Allow running as "python scripts/sync_forum_topics.py".
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common_config import ConfigManager, load_relay_settings
from structured_logger import get_logger, log_event


def _date_to_ts(value) -> int:
    if not value:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


async def _fetch_forum_topics(client: TelegramClient, peer, q: str, *, max_pages: int = 20):
    offset_date = 0
    offset_id = 0
    offset_topic = 0

    seen_offsets: set[tuple[int, int]] = set()
    out: list = []

    for _ in range(int(max_pages)):
        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=peer,
                q=q,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            )
        )
        topics = list(getattr(res, "topics", []) or [])
        if not topics:
            return out, False

        out.extend(topics)

        last = topics[-1]
        offset_date = _date_to_ts(getattr(last, "date", None)) or offset_date
        offset_topic = int(getattr(last, "id", 0) or 0)
        offset_id = int(getattr(last, "top_message", 0) or 0)

        # Safety net against infinite loops when the API doesn't advance offsets.
        key = (offset_topic, offset_id)
        if key in seen_offsets:
            return out, True
        seen_offsets.add(key)

    return out, True


def _dedupe_topics_by_id(topics: list) -> list:
    out = []
    seen: set[int] = set()
    for t in topics:
        tid = int(getattr(t, "id", 0) or 0)
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        out.append(t)
    return out


async def _list_topics_matching_title(client: TelegramClient, peer, title: str):
    topics, possibly_incomplete = await _fetch_forum_topics(client, peer, title, max_pages=20)
    topics = _dedupe_topics_by_id(topics)
    matches = [t for t in topics if getattr(t, "title", None) == title]

    if not possibly_incomplete:
        return matches

    topics, _ = await _fetch_forum_topics(client, peer, "", max_pages=200)
    topics = _dedupe_topics_by_id(topics)
    return [t for t in topics if getattr(t, "title", None) == title]


def _topic_popularity_key(topic):
    is_visible = 0 if bool(getattr(topic, "hidden", False)) else 1
    top_message = int(getattr(topic, "top_message", 0) or 0)
    tid = int(getattr(topic, "id", 0) or 0)
    return (is_visible, top_message, tid)


async def _get_forum_topic(client: TelegramClient, peer, title: str):
    topics = await _list_topics_matching_title(client, peer, title)
    if not topics:
        return None
    return max(topics, key=_topic_popularity_key)


async def _ensure_topic(client: TelegramClient, peer, chat_id: int, title: str, *, dry_run: bool, logger):
    existing = await _get_forum_topic(client, peer, title)
    if existing is not None:
        return

    if dry_run:
        log_event(logger, 20, "forum_topic_would_create", chat_id=chat_id, title=title)
        return

    await client(functions.messages.CreateForumTopicRequest(peer=peer, title=title))
    log_event(logger, 20, "forum_topic_created", chat_id=chat_id, title=title)


def _archived_duplicate_title(title: str, topic_id: int) -> str:
    return f"{title} (archived {int(topic_id)})"


async def _archive_topic_duplicate(client: TelegramClient, peer, chat_id: int, topic, *, dry_run: bool, logger):
    topic_id = int(getattr(topic, "id", 0) or 0)
    title = str(getattr(topic, "title", "") or "")
    if not topic_id or not title:
        return

    archived_title = _archived_duplicate_title(title, topic_id)

    if dry_run:
        log_event(logger, 20, "forum_topic_would_archive_duplicate", chat_id=chat_id, topic_id=topic_id, title=title, archived_title=archived_title)
        return

    await client(
        functions.messages.EditForumTopicRequest(
            peer=peer,
            topic_id=topic_id,
            title=archived_title,
            hidden=True,
            closed=True,
        )
    )
    log_event(logger, 20, "forum_topic_archived_duplicate", chat_id=chat_id, topic_id=topic_id, title=title, archived_title=archived_title)


async def _rename_topic(client: TelegramClient, peer, chat_id: int, old_title: str, new_title: str, *, dry_run: bool, logger):
    topics = await _list_topics_matching_title(client, peer, old_title)
    if not topics:
        return

    keep = max(topics, key=_topic_popularity_key)
    keep_id = int(getattr(keep, "id", 0) or 0)
    if not keep_id:
        return

    if dry_run:
        log_event(
            logger,
            20,
            "forum_topic_would_rename",
            chat_id=chat_id,
            topic_id=keep_id,
            old_title=old_title,
            new_title=new_title,
            duplicate_topic_ids=[int(getattr(t, "id", 0) or 0) for t in topics if int(getattr(t, "id", 0) or 0) and int(getattr(t, "id", 0) or 0) != keep_id],
        )
    else:
        await client(functions.messages.EditForumTopicRequest(peer=peer, topic_id=keep_id, title=new_title))
        log_event(logger, 20, "forum_topic_renamed", chat_id=chat_id, topic_id=keep_id, old_title=old_title, new_title=new_title)

    for t in topics:
        tid = int(getattr(t, "id", 0) or 0)
        if not tid or tid == keep_id:
            continue
        await _archive_topic_duplicate(client, peer, chat_id, t, dry_run=bool(dry_run), logger=logger)


async def _delete_topic_history_and_hide_one(client: TelegramClient, peer, chat_id: int, topic, *, dry_run: bool, logger):
    topic_id = int(getattr(topic, "id", 0) or 0)
    title = str(getattr(topic, "title", "") or "")
    top_message = int(getattr(topic, "top_message", 0) or 0)
    if not topic_id:
        return

    if dry_run:
        log_event(logger, 20, "forum_topic_would_delete", chat_id=chat_id, topic_id=topic_id, title=title, top_message=top_message or None)
        return

    if top_message:
        await client(functions.messages.DeleteTopicHistoryRequest(peer=peer, top_msg_id=top_message))

    await client(functions.messages.EditForumTopicRequest(peer=peer, topic_id=topic_id, hidden=True, closed=True))
    log_event(logger, 20, "forum_topic_deleted", chat_id=chat_id, topic_id=topic_id, title=title)


async def _delete_topic_history_and_hide(client: TelegramClient, peer, chat_id: int, title: str, *, dry_run: bool, logger):
    topics = await _list_topics_matching_title(client, peer, title)
    for t in topics:
        await _delete_topic_history_and_hide_one(client, peer, chat_id, t, dry_run=bool(dry_run), logger=logger)


def _as_int_keyed_map(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        try:
            out[int(k)] = v
        except Exception:
            continue
    return out


async def main() -> None:
    p = argparse.ArgumentParser(description="Create/rename/hide forum topics based on config.json")
    p.add_argument("--config", default="config.json", help="Path to config.json (default: config.json)")
    p.add_argument("--dry-run", action="store_true", help="Print actions without modifying topics")
    p.add_argument(
        "--session",
        default="topic_session",
        help="Telethon session name (default: topic_session). Stop userbot first or use a different session if you get sqlite 'database is locked'.",
    )
    p.add_argument(
        "--bot-token",
        default=None,
        help="Optional. If set, login as bot. Note: listing forum topics via GetForumTopicsRequest is restricted for bots.",
    )
    args = p.parse_args()

    logger = get_logger("topic_sync")

    cm = ConfigManager(args.config)
    cfg = cm.load(force=True)
    relay_cfg = cfg.get("relay", {}) or {}
    settings = load_relay_settings(cm)

    client = TelegramClient(str(args.session), settings["api_id"], settings["api_hash"])
    if args.bot_token:
        await client.start(bot_token=str(args.bot_token))
    else:
        await client.start()

    ensure = relay_cfg.get("ensure_forum_topics", []) or []
    renames = _as_int_keyed_map(relay_cfg.get("topic_renames", {}) or {})
    deletes = _as_int_keyed_map(relay_cfg.get("topic_deletes", {}) or {})

    for chat_id, mapping in renames.items():
        if not isinstance(mapping, dict):
            continue
        peer = await client.get_input_entity(int(chat_id))
        for old_title, new_title in mapping.items():
            if not old_title or not new_title:
                continue
            await _rename_topic(client, peer, int(chat_id), str(old_title), str(new_title), dry_run=bool(args.dry_run), logger=logger)

    for item in ensure:
        if not isinstance(item, dict) or "chat_id" not in item:
            continue
        chat_id = int(item["chat_id"])
        topics = item.get("topics", []) or []
        peer = await client.get_input_entity(chat_id)
        for title in topics:
            if not title:
                continue
            await _ensure_topic(client, peer, chat_id, str(title), dry_run=bool(args.dry_run), logger=logger)

    for chat_id, titles in deletes.items():
        if not isinstance(titles, list):
            continue
        peer = await client.get_input_entity(int(chat_id))
        for title in titles:
            if not title:
                continue
            await _delete_topic_history_and_hide(client, peer, int(chat_id), str(title), dry_run=bool(args.dry_run), logger=logger)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
