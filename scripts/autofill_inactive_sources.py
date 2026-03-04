import argparse
import asyncio
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError

from common_config import ConfigManager, load_userbot_settings
from message_filter import should_block_text
from telethon_spam import group_looks_like_promo_directory


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS used (
  source_chat_id INTEGER NOT NULL,
  source_message_id INTEGER NOT NULL,
  dest_chat_id INTEGER NOT NULL,
  dest_topic_id INTEGER NOT NULL,
  used_at INTEGER NOT NULL,
  PRIMARY KEY (source_chat_id, source_message_id, dest_chat_id, dest_topic_id)
);
"""

SRC_MARKER_PREFIX = "[[SRC:"
SRC_MARKER_SUFFIX = "]]"


BUILTIN_BLOCKLIST_SUBSTRINGS = [
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


@dataclass
class Destination:
    chat_id: int
    topic_title: str | None = None
    topic_from_source: bool = False
    bucket_cfg: dict | None = None


def _connect_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    return conn


def _topic_title_variants(title: str) -> list[str]:
    if not title:
        return []

    t = str(title)
    out = [t]

    # "🍑 Foo" -> "Foo"
    if " " in t:
        first, rest = t.split(" ", 1)
        if len(first) <= 8 and rest:
            out.append(rest)

    return out


async def _resolve_or_create_topic(client: TelegramClient, chat_id: int, title: str) -> tuple[int, int]:
    entity = await client.get_entity(chat_id)

    for q in _topic_title_variants(title):
        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=entity,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=50,
                q=str(q),
            )
        )
        for t in list(res.topics):
            if t.title == title or t.title == str(q):
                return int(t.id), int(t.top_message)

    await client(functions.messages.CreateForumTopicRequest(peer=entity, title=str(title), icon_color=0x6FB9F0))

    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=50,
            q=str(title),
        )
    )
    for t in list(res.topics):
        if t.title == title:
            return int(t.id), int(t.top_message)

    raise RuntimeError(f"topic not found after create: chat_id={chat_id} title={title!r}")


def _raw_message_text(msg) -> str:
    return (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()


def _media_for_send(msg):
    media = getattr(msg, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
        return None

    if getattr(msg, "sticker", None) is not None:
        return None

    photo = getattr(msg, "photo", None)
    if photo is not None:
        return photo

    doc = getattr(msg, "document", None)
    if doc is not None:
        mime = (getattr(doc, "mime_type", None) or "").lower()
        if mime.startswith("image/") or mime.startswith("video/"):
            return doc

    return None


def _normalize_destinations(destinations) -> list[dict]:
    if not destinations:
        return []
    out = []
    for d in destinations:
        if isinstance(d, int):
            out.append({"chat_id": int(d)})
        elif isinstance(d, dict):
            out.append(d)
    return out


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


def _resolve_destinations(relay_cfg: dict, source_peer_id: int | None) -> list[Destination]:
    if source_peer_id is not None:
        for route in relay_cfg.get("routes", []) or []:
            try:
                if "source_chat" in route and int(route.get("source_chat")) == int(source_peer_id):
                    raw = route.get("destinations") or route.get("dest_channels") or []
                    return _destinations_from_raw(raw)

                src_list = route.get("source_chats")
                if isinstance(src_list, list) and any(int(x) == int(source_peer_id) for x in src_list):
                    raw = route.get("destinations") or route.get("dest_channels") or []
                    return _destinations_from_raw(raw)
            except Exception:  # noqa: BLE001
                continue

    return _destinations_from_raw(relay_cfg.get("default_destinations"))


def _destinations_from_raw(raw) -> list[Destination]:
    out: list[Destination] = []
    for d in _normalize_destinations(raw):
        chat_id = d.get("chat_id")
        if not chat_id:
            continue

        topic_from_source = bool(d.get("topic_from_source"))

        topic_title = d.get("topic_title") or d.get("topic")
        bucket_cfg = d.get("bucket_topics") or d.get("bucket")

        if topic_title is None and bucket_cfg is None and not topic_from_source:
            continue

        out.append(
            Destination(
                chat_id=int(chat_id),
                topic_title=str(topic_title) if topic_title is not None else None,
                topic_from_source=topic_from_source,
                bucket_cfg=bucket_cfg if isinstance(bucket_cfg, dict) else None,
            )
        )
    return out


def _media_filter_kwargs(relay_cfg: dict) -> dict:
    if bool(relay_cfg.get("media_filter_use_general_blocklist", True)):
        return {
            "blocklist_substrings": BUILTIN_BLOCKLIST_SUBSTRINGS + list(relay_cfg.get("blocklist_substrings") or []),
            "blocklist_regexes": relay_cfg.get("blocklist_regexes") or [],
            "block_contact_ads": bool(relay_cfg.get("block_contact_ads", True)),
            "contact_ad_keywords": relay_cfg.get("contact_ad_keywords"),
        }

    return {
        "blocklist_substrings": BUILTIN_BLOCKLIST_SUBSTRINGS + list(relay_cfg.get("media_blocklist_substrings") or []),
        "blocklist_regexes": relay_cfg.get("media_blocklist_regexes") or [],
        "block_contact_ads": bool(relay_cfg.get("media_block_contact_ads", False)),
        "contact_ad_keywords": relay_cfg.get("media_contact_ad_keywords") or relay_cfg.get("contact_ad_keywords"),
    }


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


async def _send_media(client: TelegramClient, dest_chat_id: int, reply_to: int, media, caption: str) -> None:
    try:
        await client.send_message(dest_chat_id, message=str(caption or ""), file=media, reply_to=int(reply_to))
        return
    except ChatForwardsRestrictedError:
        tmpdir = tempfile.mkdtemp(prefix="inactive_src_")
        try:
            paths: list[str] = []
            if isinstance(media, list):
                for item in media:
                    p = await client.download_media(item, file=tmpdir)
                    if p:
                        paths.append(p)
            else:
                p = await client.download_media(media, file=tmpdir)
                if p:
                    paths.append(p)

            if not paths:
                return

            if len(paths) == 1:
                await client.send_file(dest_chat_id, paths[0], caption=str(caption or ""), reply_to=int(reply_to))
            else:
                await client.send_file(dest_chat_id, paths, caption=str(caption or ""), reply_to=int(reply_to))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


async def _iter_source_groups(client: TelegramClient, source_chat_id: int, *, limit: int):
    current_gid = None
    group: list = []

    async def flush():
        nonlocal current_gid, group
        if not group:
            return
        msgs = group
        group = []
        current_gid = None
        yield msgs

    async for m in client.iter_messages(int(source_chat_id), limit=int(limit)):
        gid = getattr(m, "grouped_id", None)
        if gid:
            if current_gid is None:
                current_gid = gid
                group = [m]
                continue
            if int(gid) == int(current_gid):
                group.append(m)
                continue

            async for x in flush():
                yield x
            current_gid = gid
            group = [m]
            continue

        if group:
            async for x in flush():
                yield x

        yield [m]

    if group:
        async for x in flush():
            yield x


async def _is_source_inactive(client: TelegramClient, source_chat_id: int, inactive_min: int) -> bool:
    if int(inactive_min) <= 0:
        return True

    last = None
    async for m in client.iter_messages(int(source_chat_id), limit=1):
        last = m
        break

    if not last or not getattr(last, "date", None):
        return True

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(inactive_min))
    return last.date < cutoff


def _used_recently(
    conn: sqlite3.Connection,
    source_chat_id: int,
    source_message_ids: list[int],
    dest_chat_id: int,
    dest_topic_id: int,
    cutoff_ts: int,
) -> bool:
    for mid in source_message_ids:
        row = conn.execute(
            "SELECT used_at FROM used WHERE source_chat_id=? AND source_message_id=? AND dest_chat_id=? AND dest_topic_id=?",
            (int(source_chat_id), int(mid), int(dest_chat_id), int(dest_topic_id)),
        ).fetchone()
        if not row:
            continue
        used_at = int(row[0] or 0)
        if used_at >= int(cutoff_ts):
            return True
    return False


def _mark_used(
    conn: sqlite3.Connection,
    source_chat_id: int,
    source_message_ids: list[int],
    dest_chat_id: int,
    dest_topic_id: int,
) -> None:
    now_ts = int(time.time())
    for mid in source_message_ids:
        conn.execute(
            "INSERT OR REPLACE INTO used(source_chat_id, source_message_id, dest_chat_id, dest_topic_id, used_at) VALUES(?,?,?,?,?)",
            (int(source_chat_id), int(mid), int(dest_chat_id), int(dest_topic_id), int(now_ts)),
        )
    conn.commit()


async def run_once(
    client: TelegramClient,
    conn: sqlite3.Connection,
    cfg: dict,
    *,
    inactive_min: int,
    fill_count: int,
    lookback_days: int,
    scan_limit: int,
    max_posts_per_run: int,
    sleep_between_posts: float,
    only_source_chat_id: int | None,
    only_dest_chat_id: int | None,
    only_topic_title: str | None,
    dry_run: bool,
) -> None:
    relay_cfg = cfg.get("relay") or {}
    bot_mappings = cfg.get("bot_mappings") or []

    sources: list[int] = []
    for m in bot_mappings:
        try:
            sources.append(int(m.get("source_chat")))
        except Exception:  # noqa: BLE001
            continue

    if only_source_chat_id is not None:
        sources = [s for s in sources if int(s) == int(only_source_chat_id)]

    now_ts = int(time.time())
    cutoff_ts = int(now_ts) - (int(lookback_days) * 86400)

    posted = 0

    for src in sources:
        if posted >= int(max_posts_per_run):
            return

        if not await _is_source_inactive(client, src, inactive_min=int(inactive_min)):
            continue

        destinations = _resolve_destinations(relay_cfg, source_peer_id=int(src))
        if only_dest_chat_id is not None:
            destinations = [d for d in destinations if int(d.chat_id) == int(only_dest_chat_id)]

        if not destinations:
            continue

        need_source_title = any(d.topic_from_source for d in destinations)
        source_title = None
        if need_source_title:
            try:
                ent = await client.get_entity(int(src))
                source_title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(src)
            except Exception:  # noqa: BLE001
                source_title = str(src)

        # Resolve destination topics once per destination per source.
        # For bucket topics we resolve per message, but we keep a small cache.
        topic_cache: dict[tuple[int, str], tuple[int, int]] = {}

        candidates: list[list] = []
        async for group in _iter_source_groups(client, int(src), limit=int(scan_limit)):
            msgs = sorted(group, key=lambda x: int(x.id))
            msg_ids = [int(x.id) for x in msgs]

            media_items = []
            for m in msgs:
                media = _media_for_send(m)
                if media is not None:
                    media_items.append(media)

            if not media_items:
                continue

            if group_looks_like_promo_directory(msgs):
                continue

            album_text = "\n".join([_raw_message_text(m) for m in msgs if _raw_message_text(m)])
            if album_text and should_block_text(album_text, **_media_filter_kwargs(relay_cfg)):
                continue

            candidates.append(msgs)

        if not candidates:
            continue

        random.shuffle(candidates)

        to_post = int(fill_count)
        for group_msgs in candidates:
            if posted >= int(max_posts_per_run):
                return
            if to_post <= 0:
                break

            group_msgs = sorted(group_msgs, key=lambda x: int(x.id))
            first_id = int(group_msgs[0].id)
            msg_ids = [int(x.id) for x in group_msgs]

            for dest in destinations:
                if posted >= int(max_posts_per_run):
                    return

                topic_title = dest.topic_title
                if dest.topic_from_source:
                    topic_title = str(source_title or src)

                if dest.bucket_cfg:
                    topic_title = _bucket_topic_title(dest.bucket_cfg, source_peer_id=int(src), message_id=first_id)

                if not topic_title:
                    continue

                if only_topic_title is not None and str(topic_title) != str(only_topic_title):
                    continue

                key = (int(dest.chat_id), str(topic_title))
                if key not in topic_cache:
                    dest_topic_id, dest_top_message = await _resolve_or_create_topic(client, int(dest.chat_id), str(topic_title))
                    topic_cache[key] = (int(dest_topic_id), int(dest_top_message))

                dest_topic_id, dest_top_message = topic_cache[key]

                if _used_recently(conn, int(src), msg_ids, int(dest.chat_id), int(dest_topic_id), cutoff_ts=int(cutoff_ts)):
                    continue

                override_caption = _pick_post_caption(relay_cfg.get("post_captions"), int(dest.chat_id))
                if override_caption:
                    caption = override_caption
                elif bool(relay_cfg.get("strip_text", True)):
                    caption = ""
                else:
                    caption = _raw_message_text(group_msgs[0])

                media_items = []
                for m in group_msgs:
                    media = _media_for_send(m)
                    if media is not None:
                        media_items.append(media)
                if not media_items:
                    continue

                if dry_run:
                    print(
                        f"DRY_RUN send: source_chat_id={int(src)} msg_ids={msg_ids} -> dest_chat_id={int(dest.chat_id)} topic={str(topic_title)!r}"
                    )
                else:
                    try:
                        await _send_media(
                            client,
                            dest_chat_id=int(dest.chat_id),
                            reply_to=int(dest_top_message),
                            media=media_items if len(media_items) > 1 else media_items[0],
                            caption=caption,
                        )
                    except FloodWaitError as e:
                        await asyncio.sleep(int(e.seconds) + 1)
                        await _send_media(
                            client,
                            dest_chat_id=int(dest.chat_id),
                            reply_to=int(dest_top_message),
                            media=media_items if len(media_items) > 1 else media_items[0],
                            caption=caption,
                        )

                    _mark_used(conn, int(src), msg_ids, int(dest.chat_id), int(dest_topic_id))

                posted += 1
                to_post -= 1
                await asyncio.sleep(float(sleep_between_posts))

                if to_post <= 0:
                    break


async def main():
    parser = argparse.ArgumentParser(
        description="If a source channel/group is inactive, repost older media from that source into its configured destination topic(s)."
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--db", default="data/inactive_sources.sqlite3", help="SQLite db path for repost tracking")

    parser.add_argument("--inactive-min", type=int, default=60, help="Treat a source as inactive if it had no posts in the last N minutes")
    parser.add_argument("--fill-count", type=int, default=1, help="How many posts to add per inactive source")
    parser.add_argument("--lookback-days", type=int, default=30, help="Avoid reposting the same source message within N days")

    parser.add_argument("--scan-limit", type=int, default=2000, help="How many recent source messages to scan")
    parser.add_argument("--max-posts-per-run", type=int, default=50, help="Safety cap")
    parser.add_argument("--sleep-between-posts", type=float, default=1.0, help="Delay between posts")

    parser.add_argument("--only-source-chat-id", type=int, help="Only process a single source chat")
    parser.add_argument("--only-dest-chat-id", type=int, help="Only post into this destination chat")
    parser.add_argument("--only-topic-title", help="Only post into this destination topic title")

    parser.add_argument("--dry-run", action="store_true", help="Do not send anything")

    parser.add_argument("--daemon", action="store_true", help="Run forever")
    parser.add_argument("--interval-min", type=int, default=60, help="Loop interval in minutes for --daemon")

    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    settings = load_userbot_settings(config_manager)
    session_dir = Path("data/sessions")
    session_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session_dir / "userbot_tools"),
        settings["api_id"],
        settings["api_hash"],
        proxy=settings["proxy"],
    )
    await client.start()

    conn = _connect_db(args.db)

    try:
        while True:
            await run_once(
                client,
                conn,
                cfg,
                inactive_min=int(args.inactive_min),
                fill_count=int(args.fill_count),
                lookback_days=int(args.lookback_days),
                scan_limit=int(args.scan_limit),
                max_posts_per_run=int(args.max_posts_per_run),
                sleep_between_posts=float(args.sleep_between_posts),
                only_source_chat_id=args.only_source_chat_id,
                only_dest_chat_id=args.only_dest_chat_id,
                only_topic_title=str(args.only_topic_title) if args.only_topic_title else None,
                dry_run=bool(args.dry_run),
            )

            if not args.daemon:
                return
            await asyncio.sleep(int(args.interval_min) * 60)
    finally:
        conn.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
