import argparse
import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient, functions

# Allow running as "python scripts/sync_forum_topics.py".
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common_config import ConfigManager, load_relay_settings
from structured_logger import get_logger, log_event


async def _get_forum_topic(client: TelegramClient, peer, title: str):
    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=peer,
            q=title,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
        )
    )
    for t in getattr(res, "topics", []) or []:
        if getattr(t, "title", None) == title:
            return t
    return None


async def _ensure_topic(client: TelegramClient, peer, chat_id: int, title: str, *, dry_run: bool, logger):
    existing = await _get_forum_topic(client, peer, title)
    if existing is not None:
        return

    if dry_run:
        log_event(logger, 20, "forum_topic_would_create", chat_id=chat_id, title=title)
        return

    await client(
        functions.messages.CreateForumTopicRequest(
            peer=peer,
            title=title,
        )
    )
    log_event(logger, 20, "forum_topic_created", chat_id=chat_id, title=title)


async def _rename_topic(client: TelegramClient, peer, chat_id: int, old_title: str, new_title: str, *, dry_run: bool, logger):
    t = await _get_forum_topic(client, peer, old_title)
    if t is None:
        return

    topic_id = int(getattr(t, "id", 0) or 0)
    if not topic_id:
        return

    if dry_run:
        log_event(
            logger,
            20,
            "forum_topic_would_rename",
            chat_id=chat_id,
            topic_id=topic_id,
            old_title=old_title,
            new_title=new_title,
        )
        return

    await client(
        functions.messages.EditForumTopicRequest(
            peer=peer,
            topic_id=topic_id,
            title=new_title,
        )
    )
    log_event(logger, 20, "forum_topic_renamed", chat_id=chat_id, topic_id=topic_id, old_title=old_title, new_title=new_title)


async def _delete_topic_history_and_hide(client: TelegramClient, peer, chat_id: int, title: str, *, dry_run: bool, logger):
    t = await _get_forum_topic(client, peer, title)
    if t is None:
        return

    topic_id = int(getattr(t, "id", 0) or 0)
    top_message = int(getattr(t, "top_message", 0) or 0)
    if not topic_id:
        return

    if dry_run:
        log_event(
            logger,
            20,
            "forum_topic_would_delete",
            chat_id=chat_id,
            topic_id=topic_id,
            title=title,
            top_message=top_message or None,
        )
        return

    # Best-effort: clear history (removes messages in the topic), then hide it.
    if top_message:
        await client(
            functions.messages.DeleteTopicHistoryRequest(
                peer=peer,
                top_msg_id=top_message,
            )
        )

    await client(
        functions.messages.EditForumTopicRequest(
            peer=peer,
            topic_id=topic_id,
            hidden=True,
            closed=True,
        )
    )
    log_event(logger, 20, "forum_topic_deleted", chat_id=chat_id, topic_id=topic_id, title=title)


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
        default="bot_session",
        help="Telethon session name (default: bot_session). Stop relaybot first or use a different session if you get sqlite 'database is locked'.",
    )
    args = p.parse_args()

    logger = get_logger("topic_sync")

    cm = ConfigManager(args.config)
    cfg = cm.load(force=True)
    relay_cfg = cfg.get("relay", {}) or {}
    settings = load_relay_settings(cm)

    client = TelegramClient(str(args.session), settings["api_id"], settings["api_hash"])
    await client.start(bot_token=settings["bot_token"])

    ensure = relay_cfg.get("ensure_forum_topics", []) or []
    renames = _as_int_keyed_map(relay_cfg.get("topic_renames", {}) or {})
    deletes = _as_int_keyed_map(relay_cfg.get("topic_deletes", {}) or {})

    # 1) renames first (so ensure runs on final names)
    for chat_id, mapping in renames.items():
        if not isinstance(mapping, dict):
            continue
        peer = await client.get_input_entity(int(chat_id))
        for old_title, new_title in mapping.items():
            if not old_title or not new_title:
                continue
            await _rename_topic(
                client,
                peer,
                int(chat_id),
                str(old_title),
                str(new_title),
                dry_run=bool(args.dry_run),
                logger=logger,
            )

    # 2) ensure topics exist
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

    # 3) deletions (hide + clear history)
    for chat_id, titles in deletes.items():
        if not isinstance(titles, list):
            continue
        peer = await client.get_input_entity(int(chat_id))
        for title in titles:
            if not title:
                continue
            await _delete_topic_history_and_hide(
                client,
                peer,
                int(chat_id),
                str(title),
                dry_run=bool(args.dry_run),
                logger=logger,
            )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
