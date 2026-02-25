import argparse
import asyncio
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
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
CREATE TABLE IF NOT EXISTS backfill_used (
  source_chat_id INTEGER NOT NULL,
  source_message_id INTEGER NOT NULL,
  dest_chat_id INTEGER NOT NULL,
  dest_topic_id INTEGER NOT NULL,
  used_at INTEGER NOT NULL,
  PRIMARY KEY (source_chat_id, source_message_id, dest_chat_id, dest_topic_id)
);
"""


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
    topic_title: str


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


def _media_filter_kwargs(relay: dict) -> dict:
    if bool(relay.get("media_filter_use_general_blocklist", True)):
        return {
            "blocklist_substrings": BUILTIN_BLOCKLIST_SUBSTRINGS + list(relay.get("blocklist_substrings") or []),
            "blocklist_regexes": relay.get("blocklist_regexes") or [],
            "block_contact_ads": bool(relay.get("block_contact_ads", True)),
            "contact_ad_keywords": relay.get("contact_ad_keywords"),
        }

    return {
        "blocklist_substrings": BUILTIN_BLOCKLIST_SUBSTRINGS + list(relay.get("media_blocklist_substrings") or []),
        "blocklist_regexes": relay.get("media_blocklist_regexes") or [],
        "block_contact_ads": bool(relay.get("media_block_contact_ads", False)),
        "contact_ad_keywords": relay.get("media_contact_ad_keywords") or relay.get("contact_ad_keywords"),
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


def _mark_used(conn: sqlite3.Connection, source_chat_id: int, source_message_ids: list[int], dest_chat_id: int, dest_topic_id: int) -> None:
    now_ts = int(time.time())
    for mid in source_message_ids:
        conn.execute(
            "INSERT OR REPLACE INTO backfill_used(source_chat_id, source_message_id, dest_chat_id, dest_topic_id, used_at) VALUES(?,?,?,?,?)",
            (int(source_chat_id), int(mid), int(dest_chat_id), int(dest_topic_id), int(now_ts)),
        )
    conn.commit()


def _is_used(conn: sqlite3.Connection, source_chat_id: int, source_message_ids: list[int], dest_chat_id: int, dest_topic_id: int) -> bool:
    for mid in source_message_ids:
        row = conn.execute(
            "SELECT 1 FROM backfill_used WHERE source_chat_id=? AND source_message_id=? AND dest_chat_id=? AND dest_topic_id=?",
            (int(source_chat_id), int(mid), int(dest_chat_id), int(dest_topic_id)),
        ).fetchone()
        if row:
            return True
    return False


async def _send_media(
    client: TelegramClient,
    dest_chat_id: int,
    reply_to: int,
    media,
    caption: str,
) -> None:
    try:
        await client.send_message(dest_chat_id, message=str(caption or ""), file=media, reply_to=int(reply_to))
        return
    except ChatForwardsRestrictedError:
        tmpdir = tempfile.mkdtemp(prefix="backfill_")
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


def _route_sources(route: dict) -> list[int]:
    if "source_chat" in route:
        try:
            return [int(route.get("source_chat"))]
        except Exception:  # noqa: BLE001
            return []

    srcs = route.get("source_chats")
    if isinstance(srcs, list):
        out: list[int] = []
        for x in srcs:
            try:
                out.append(int(x))
            except Exception:  # noqa: BLE001
                continue
        return out

    return []


def _route_destinations(route: dict) -> list[Destination]:
    out: list[Destination] = []
    for d in _normalize_destinations(route.get("destinations") or route.get("dest_channels") or []):
        chat_id = d.get("chat_id")
        title = d.get("topic_title") or d.get("topic")
        if not chat_id or not title:
            continue
        out.append(Destination(chat_id=int(chat_id), topic_title=str(title)))
    return out


async def backfill_one_destination(
    client: TelegramClient,
    conn: sqlite3.Connection,
    *,
    source_chat_id: int,
    dest: Destination,
    relay_cfg: dict,
    scan_limit: int,
    max_posts: int,
    sleep_between_posts: float,
    dry_run: bool,
) -> int:
    dest_topic_id, dest_top_message = await _resolve_or_create_topic(client, dest.chat_id, dest.topic_title)

    posted = 0

    async for group in _iter_source_groups(client, source_chat_id, limit=scan_limit):
        msgs = sorted(group, key=lambda x: int(x.id))
        msg_ids = [int(m.id) for m in msgs]

        if _is_used(conn, source_chat_id, msg_ids, dest.chat_id, dest_topic_id):
            continue

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

        override_caption = _pick_post_caption(relay_cfg.get("post_captions"), dest.chat_id)
        if override_caption:
            caption = override_caption
        elif bool(relay_cfg.get("strip_text", True)):
            caption = ""
        else:
            caption = _raw_message_text(msgs[0])

        if dry_run:
            print(
                f"DRY_RUN send: source_chat_id={source_chat_id} msg_ids={msg_ids} -> dest_chat_id={dest.chat_id} topic={dest.topic_title!r}"
            )
            posted += 1
        else:
            try:
                await _send_media(
                    client,
                    dest_chat_id=dest.chat_id,
                    reply_to=dest_top_message,
                    media=media_items if len(media_items) > 1 else media_items[0],
                    caption=caption,
                )
            except FloodWaitError as e:
                await asyncio.sleep(int(e.seconds) + 1)
                await _send_media(
                    client,
                    dest_chat_id=dest.chat_id,
                    reply_to=dest_top_message,
                    media=media_items if len(media_items) > 1 else media_items[0],
                    caption=caption,
                )

            _mark_used(conn, source_chat_id, msg_ids, dest.chat_id, dest_topic_id)
            posted += 1
            await asyncio.sleep(float(sleep_between_posts))

        if posted >= int(max_posts):
            return posted

    return posted


async def main():
    parser = argparse.ArgumentParser(description="Backfill media from source channels into forum topics (based on relay.routes)")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--db", default="data/backfill.sqlite3", help="SQLite db path for backfill tracking")

    parser.add_argument("--source-chat-id", type=int, action="append", help="Only backfill these source chats (can repeat)")
    parser.add_argument("--dest-chat-id", type=int, action="append", help="Only backfill into these destination chats (can repeat)")
    parser.add_argument("--only-topic-title", help="Only backfill into this destination topic title (exact match)")

    parser.add_argument("--scan-limit", type=int, default=2000, help="How many recent source messages to scan")
    parser.add_argument("--max-posts", type=int, default=10, help="Max posts to send per destination")
    parser.add_argument("--sleep-between-posts", type=float, default=1.0, help="Delay between posts")
    parser.add_argument("--dry-run", action="store_true", help="Do not send anything, only print what would be sent")

    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    relay_cfg = cfg.get("relay") or {}
    routes = relay_cfg.get("routes") or []
    if not routes:
        raise SystemExit("relay.routes is empty")

    only_sources = [int(x) for x in (args.source_chat_id or [])]
    only_dests = [int(x) for x in (args.dest_chat_id or [])]
    only_title = str(args.only_topic_title) if args.only_topic_title else None

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
        for route in routes:
            srcs = _route_sources(route)
            if not srcs:
                continue

            dests = _route_destinations(route)
            if not dests:
                continue

            if only_sources:
                srcs = [s for s in srcs if any(int(s) == int(x) for x in only_sources)]
                if not srcs:
                    continue

            if only_dests:
                dests = [d for d in dests if any(int(d.chat_id) == int(x) for x in only_dests)]
                if not dests:
                    continue

            if only_title:
                dests = [d for d in dests if d.topic_title == only_title]
                if not dests:
                    continue

            for src in srcs:
                for dest in dests:
                    posted = await backfill_one_destination(
                        client,
                        conn,
                        source_chat_id=int(src),
                        dest=dest,
                        relay_cfg=relay_cfg,
                        scan_limit=int(args.scan_limit),
                        max_posts=int(args.max_posts),
                        sleep_between_posts=float(args.sleep_between_posts),
                        dry_run=bool(args.dry_run),
                    )
                    print(
                        f"backfill_done: source_chat_id={int(src)} dest_chat_id={dest.chat_id} topic={dest.topic_title!r} posted={int(posted)}"
                    )
    finally:
        conn.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
