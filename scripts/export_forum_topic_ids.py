import argparse
import asyncio
import json
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, functions

# Allow running as "python scripts/export_forum_topic_ids.py".
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common_config import ConfigManager, load_relay_settings
from structured_logger import get_logger, log_event


def _normalize_title(title: str) -> str:
    # Keep in sync with bot_relay.normalize_forum_topic_title / common_config.
    s = unicodedata.normalize("NFKC", str(title or "")).strip()
    s = s.replace("\ufe0f", "").replace("\ufe0e", "")

    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        cat = unicodedata.category(ch)
        if cat in {"So", "Sk", "Cf"}:
            i += 1
            continue
        break
    s = s[i:].lstrip()

    return " ".join(s.split()).casefold()


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
    # Pinned first, then active.
    return (pinned, top_message, date_ts, topic_id)


async def _iter_all_topics(client: TelegramClient, peer, *, max_pages: int = 200):
    offset_date = 0
    offset_id = 0
    offset_topic = 0

    seen_offsets: set[tuple[int, int]] = set()

    for _ in range(int(max_pages)):
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
            return

        for t in topics:
            yield t

        last = topics[-1]
        offset_date = _date_to_ts(getattr(last, "date", None)) or offset_date
        offset_topic = int(getattr(last, "id", 0) or 0)
        offset_id = int(getattr(last, "top_message", 0) or 0)

        key = (offset_topic, offset_id)
        if key in seen_offsets:
            return
        seen_offsets.add(key)


def _collect_required_titles(cfg: dict) -> dict[int, set[str]]:
    relay = cfg.get("relay", {}) or {}
    required: dict[int, set[str]] = defaultdict(set)

    def add(chat_id, title):
        if chat_id is None or title is None:
            return
        t = str(title).strip()
        if not t:
            return
        required[int(chat_id)].add(t)

    for r in relay.get("routes", []) or []:
        if not isinstance(r, dict):
            continue
        for d in r.get("destinations", []) or []:
            if not isinstance(d, dict):
                continue
            add(d.get("chat_id"), d.get("topic_title"))

    for d in relay.get("default_destinations", []) or []:
        if not isinstance(d, dict):
            continue
        add(d.get("chat_id"), d.get("topic_title"))

    for item in relay.get("ensure_forum_topics", []) or []:
        if not isinstance(item, dict) or "chat_id" not in item:
            continue
        cid = int(item["chat_id"])
        for t in item.get("topics", []) or []:
            add(cid, t)

    for cid, topics in (relay.get("general_topic_buckets") or {}).items():
        if not isinstance(topics, list):
            continue
        for t in topics:
            add(cid, t)

    for cid, title in (relay.get("fallback_topic_titles") or {}).items():
        add(cid, title)

    return required


async def main() -> None:
    p = argparse.ArgumentParser(description="Export forum topic top_message ids into relay.forum_topic_ids")
    p.add_argument("--config", default="config.json", help="Path to config.json (default: config.json)")
    p.add_argument(
        "--session",
        default="topic_session",
        help="Telethon session name (default: topic_session). Stop userbot if you use the same session to avoid sqlite locks.",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="Write relay.forum_topic_ids back into config.json (otherwise prints JSON to stdout)",
    )
    args = p.parse_args()

    logger = get_logger("topic_export")

    cm = ConfigManager(args.config)
    cfg = cm.load(force=True)
    settings = load_relay_settings(cm)

    required = _collect_required_titles(cfg)
    if not required:
        log_event(logger, 30, "no_required_forum_topics")
        return

    client = TelegramClient(str(args.session), settings["api_id"], settings["api_hash"])
    await client.start()  # user login

    forum_topic_ids_out: dict[str, dict[str, int]] = {}

    for chat_id, wanted_titles in sorted(required.items()):
        peer = await client.get_input_entity(int(chat_id))

        by_norm: dict[str, list] = defaultdict(list)
        async for t in _iter_all_topics(client, peer):
            raw_title = str(getattr(t, "title", "") or "")
            norm = _normalize_title(raw_title)
            if norm:
                by_norm[norm].append(t)

        chat_map: dict[str, int] = {}
        for title in sorted(wanted_titles):
            norm = _normalize_title(title)
            matches = by_norm.get(norm, [])
            if not matches:
                log_event(logger, 30, "forum_topic_not_found", chat_id=int(chat_id), title=str(title))
                continue

            best = max(matches, key=_topic_popularity_key)
            top_message = int(getattr(best, "top_message", 0) or 0)
            if not top_message:
                log_event(logger, 30, "forum_topic_missing_top_message", chat_id=int(chat_id), title=str(title), topic_id=int(getattr(best, "id", 0) or 0))
                continue

            chat_map[str(title)] = top_message

        if chat_map:
            forum_topic_ids_out[str(chat_id)] = chat_map

    if args.write:
        relay = cfg.get("relay", {}) or {}
        relay["forum_topic_ids"] = forum_topic_ids_out
        cfg["relay"] = relay
        cm.save(cfg)
        log_event(logger, 20, "forum_topic_ids_written", chat_count=len(forum_topic_ids_out))
    else:
        print(json.dumps({"forum_topic_ids": forum_topic_ids_out}, ensure_ascii=False, indent=2))

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
