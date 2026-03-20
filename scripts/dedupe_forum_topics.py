"""scripts/dedupe_forum_topics.py

Telegram forums (topics) are not first-class “channels” in MTProto: there is no
reliable API to permanently delete a forum topic itself.

What we *can* do:
- Optionally clear the topic history (delete messages) via DeleteTopicHistory.
- Hide + close the topic via EditForumTopic.
- Rename the topic so only one topic keeps the “real” title.

This script uses those operations to (1) dedupe duplicate topic titles and
(2) optionally keep only the top N most “popular” topics.

Popularity metric (descending): pinned, top_message, date, id.
"""

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, functions

# Allow running as "python scripts/dedupe_forum_topics.py".
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common_config import ConfigManager, load_relay_settings
from structured_logger import get_logger, log_event


def _normalize_title(title: str) -> str:
    # Strip surrounding whitespace, and collapse consecutive whitespace.
    return re.sub(r"\s+", " ", str(title or "").strip())


def _date_to_ts(value) -> int:
    if not value:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp())
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


def _topic_popularity_key(topic) -> tuple[int, int, int, int]:
    pinned = 1 if bool(getattr(topic, "pinned", False)) else 0
    top_message = int(getattr(topic, "top_message", 0) or 0)
    date_ts = _date_to_ts(getattr(topic, "date", None))
    topic_id = int(getattr(topic, "id", 0) or 0)
    return (pinned, top_message, date_ts, topic_id)


async def _iter_all_topics(client: TelegramClient, peer, *, logger=None, chat_id: int | None = None):
    offset_date = 0
    offset_id = 0
    offset_topic = 0

    seen_topic_ids: set[int] = set()

    while True:
        prev = (offset_date, offset_id, offset_topic)

        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=peer,
                q="",
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            )
        )
        topics = list(getattr(res, "topics", []) or [])
        if not topics:
            break

        for t in topics:
            tid = int(getattr(t, "id", 0) or 0)
            if tid and tid in seen_topic_ids:
                continue
            if tid:
                seen_topic_ids.add(tid)
            yield t

        last = topics[-1]
        # Telethon topic.date is usually a datetime; GetForumTopicsRequest expects an
        # integer timestamp.
        offset_date = _date_to_ts(getattr(last, "date", None)) or offset_date
        offset_id = int(getattr(last, "top_message", 0) or 0)
        offset_topic = int(getattr(last, "id", 0) or 0)

        cur = (offset_date, offset_id, offset_topic)
        if cur == prev:
            if logger is not None:
                log_event(
                    logger,
                    30,
                    "forum_topic_pagination_stalled",
                    chat_id=chat_id,
                    offset_topic=offset_topic,
                    offset_id=offset_id,
                )
            break


def _infer_chat_ids_from_settings(settings: dict) -> list[int]:
    chat_ids: set[int] = set()

    for item in settings.get("ensure_forum_topics", []) or []:
        if not isinstance(item, dict) or "chat_id" not in item:
            continue
        try:
            chat_ids.add(int(item["chat_id"]))
        except Exception:  # noqa: BLE001
            pass

    for chat_id in (settings.get("topic_renames", {}) or {}).keys():
        try:
            chat_ids.add(int(chat_id))
        except Exception:  # noqa: BLE001
            pass

    for chat_id in (settings.get("topic_deletes", {}) or {}).keys():
        try:
            chat_ids.add(int(chat_id))
        except Exception:  # noqa: BLE001
            pass

    for chat_id in (settings.get("general_topic_buckets", {}) or {}).keys():
        try:
            chat_ids.add(int(chat_id))
        except Exception:  # noqa: BLE001
            pass

    for chat_id in (settings.get("fallback_topic_titles", {}) or {}).keys():
        try:
            chat_ids.add(int(chat_id))
        except Exception:  # noqa: BLE001
            pass

    for dest in settings.get("default_destinations", []) or []:
        try:
            chat_ids.add(int(dest.get("chat_id")))
        except Exception:  # noqa: BLE001
            pass

    for r in settings.get("routes", []) or []:
        for dest in r.get("destinations", []) or []:
            try:
                chat_ids.add(int(dest.get("chat_id")))
            except Exception:  # noqa: BLE001
                pass

    return sorted(chat_ids)


def _protected_titles_for_chat(settings: dict, chat_id: int) -> set[str]:
    titles: set[str] = set()

    for item in settings.get("ensure_forum_topics", []) or []:
        if int(item.get("chat_id", 0) or 0) != int(chat_id):
            continue
        for t in item.get("topics", []) or []:
            norm = _normalize_title(str(t))
            if norm:
                titles.add(norm)

    for r in settings.get("routes", []) or []:
        for dest in r.get("destinations", []) or []:
            if int(dest.get("chat_id", 0) or 0) != int(chat_id):
                continue
            norm = _normalize_title(str(dest.get("topic_title", "") or ""))
            if norm:
                titles.add(norm)

    for t in (settings.get("general_topic_buckets", {}) or {}).get(int(chat_id), []) or []:
        norm = _normalize_title(str(t))
        if norm:
            titles.add(norm)

    for dest in settings.get("default_destinations", []) or []:
        if int(dest.get("chat_id", 0) or 0) != int(chat_id):
            continue
        norm = _normalize_title(str(dest.get("topic_title", "") or ""))
        if norm:
            titles.add(norm)

    fallback = (settings.get("fallback_topic_titles", {}) or {}).get(int(chat_id))
    if fallback:
        norm = _normalize_title(str(fallback))
        if norm:
            titles.add(norm)

    return titles


def _archived_title(base_title: str, topic_id: int, *, max_len: int = 128) -> str:
    base = _normalize_title(base_title)
    prefix = "[ARCHIVED] "
    suffix = f" #{topic_id}"

    # Telegram topic title limit is not clearly documented, but 128 is a safe cap.
    budget = max_len - len(prefix) - len(suffix)
    if budget < 0:
        budget = 0

    if len(base) > budget:
        base = base[:budget].rstrip()

    return f"{prefix}{base}{suffix}".strip()


async def _rename_topic(
    client: TelegramClient,
    peer,
    chat_id: int,
    topic,
    *,
    new_title: str,
    dry_run: bool,
    logger,
):
    topic_id = int(getattr(topic, "id", 0) or 0)
    if not topic_id:
        return

    old_title = str(getattr(topic, "title", "") or "")
    if old_title == new_title:
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


async def _archive_topic(
    client: TelegramClient,
    peer,
    chat_id: int,
    topic,
    *,
    base_title: str,
    dry_run: bool,
    delete_history: bool,
    logger,
):
    topic_id = int(getattr(topic, "id", 0) or 0)
    top_message = int(getattr(topic, "top_message", 0) or 0)
    current_title = str(getattr(topic, "title", "") or "")
    if not topic_id:
        return

    new_title = _archived_title(base_title or current_title, topic_id)

    if dry_run:
        log_event(
            logger,
            20,
            "forum_topic_would_archive",
            chat_id=chat_id,
            topic_id=topic_id,
            title=current_title,
            new_title=new_title,
            top_message=top_message or None,
            delete_history=bool(delete_history),
        )
        return

    if delete_history and top_message:
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
            title=new_title,
            hidden=True,
            closed=True,
        )
    )
    log_event(logger, 20, "forum_topic_archived", chat_id=chat_id, topic_id=topic_id, title=current_title, new_title=new_title)


async def main() -> None:
    p = argparse.ArgumentParser(description="Dedupe and archive Telegram forum topics")
    p.add_argument("--config", default="config.json", help="Path to config.json (default: config.json)")
    p.add_argument("--chat-id", action="append", default=[], help="Forum chat id to process (can be repeated)")
    p.add_argument(
        "--keep-top",
        type=int,
        default=0,
        help="If >0: keep only top N topics by popularity + protected titles (default: 0)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode (default: enabled). Use --no-dry-run to apply changes.",
    )
    p.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Apply changes.",
    )
    p.add_argument(
        "--delete-history",
        action="store_true",
        help="Also clear topic history before archiving (requires topic top_message)",
    )
    p.add_argument(
        "--session",
        default="bot_session",
        help="Telethon session name (default: bot_session). Stop relaybot first or use a different session if you get sqlite 'database is locked'.",
    )
    args = p.parse_args()

    logger = get_logger("topic_dedupe")

    cm = ConfigManager(args.config)
    settings = load_relay_settings(cm)

    if args.chat_id:
        chat_ids = [int(x) for x in args.chat_id]
    else:
        chat_ids = _infer_chat_ids_from_settings(settings)

    if not chat_ids:
        log_event(logger, 30, "no_forum_chats_to_dedupe")
        return

    client = TelegramClient(str(args.session), settings["api_id"], settings["api_hash"])
    await client.start(bot_token=settings["bot_token"])

    for chat_id in chat_ids:
        peer = await client.get_input_entity(int(chat_id))

        topics: list = []
        async for t in _iter_all_topics(client, peer, logger=logger, chat_id=int(chat_id)):
            topics.append(t)

        log_event(logger, 20, "forum_topics_listed", chat_id=chat_id, topic_count=len(topics))

        by_norm_title: dict[str, list] = defaultdict(list)
        for t in topics:
            raw_title = str(getattr(t, "title", "") or "")
            norm_title = _normalize_title(raw_title)
            if not norm_title:
                continue
            by_norm_title[norm_title].append(t)

        winners_by_norm: dict[str, object] = {}
        to_archive: list[tuple[object, str]] = []
        to_rename_winners: list[tuple[object, str]] = []

        for norm_title, group in by_norm_title.items():
            if len(group) == 1:
                winners_by_norm[norm_title] = group[0]
                continue

            group_sorted = sorted(group, key=_topic_popularity_key, reverse=True)
            keep = group_sorted[0]
            keep_id = int(getattr(keep, "id", 0) or 0)

            log_event(
                logger,
                20,
                "forum_topic_dedupe_found",
                chat_id=chat_id,
                title=norm_title,
                keep_topic_id=keep_id,
                duplicate_topic_ids=[int(getattr(x, "id", 0) or 0) for x in group_sorted[1:]],
            )

            winners_by_norm[norm_title] = keep

            keep_raw_title = str(getattr(keep, "title", "") or "")
            if keep_raw_title != norm_title:
                to_rename_winners.append((keep, norm_title))

            for dup in group_sorted[1:]:
                to_archive.append((dup, norm_title))

        # Archive duplicates *before* renaming the winner topic. If the winner only
        # differs by whitespace ("Title " -> "Title"), renaming first may fail since
        # another duplicate may already have the normalized title.
        archived_ids: set[int] = set()
        for topic, base_title in to_archive:
            tid = int(getattr(topic, "id", 0) or 0)
            if tid and tid in archived_ids:
                continue
            await _archive_topic(
                client,
                peer,
                chat_id,
                topic,
                base_title=base_title,
                dry_run=bool(args.dry_run),
                delete_history=bool(args.delete_history),
                logger=logger,
            )
            if tid:
                archived_ids.add(tid)

        if to_rename_winners:
            for topic, desired_title in to_rename_winners:
                await _rename_topic(
                    client,
                    peer,
                    chat_id,
                    topic,
                    new_title=desired_title,
                    dry_run=bool(args.dry_run),
                    logger=logger,
                )

        if int(args.keep_top or 0) > 0:
            protected = _protected_titles_for_chat(settings, int(chat_id))

            winners = list(winners_by_norm.items())
            winners_sorted = sorted((t for _, t in winners), key=_topic_popularity_key, reverse=True)
            top_n = winners_sorted[: int(args.keep_top)]

            keep_ids: set[int] = {int(getattr(t, "id", 0) or 0) for t in top_n}
            for norm_title, t in winners:
                if norm_title in protected:
                    keep_ids.add(int(getattr(t, "id", 0) or 0))

            to_archive_keep_top = []
            for norm_title, t in winners:
                tid = int(getattr(t, "id", 0) or 0)
                if tid and tid in keep_ids:
                    continue
                to_archive_keep_top.append((t, norm_title))

            log_event(
                logger,
                20,
                "forum_topic_keep_top_plan",
                chat_id=chat_id,
                keep_top=int(args.keep_top),
                protected_title_count=len(protected),
                unique_topic_count=len(winners),
                keep_topic_count=len(keep_ids),
                archive_topic_count=len(to_archive_keep_top),
            )

            for topic, base_title in to_archive_keep_top:
                tid = int(getattr(topic, "id", 0) or 0)
                if tid and tid in archived_ids:
                    continue
                await _archive_topic(
                    client,
                    peer,
                    chat_id,
                    topic,
                    base_title=base_title,
                    dry_run=bool(args.dry_run),
                    delete_history=bool(args.delete_history),
                    logger=logger,
                )
                if tid:
                    archived_ids.add(tid)

        if not to_archive and int(args.keep_top or 0) <= 0:
            log_event(logger, 20, "forum_topic_dedupe_none", chat_id=chat_id)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
