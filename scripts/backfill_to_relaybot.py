import argparse
import asyncio
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, types
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError

from common_config import ConfigManager, load_userbot_settings
from message_filter import should_block_text
from telethon_spam import group_looks_like_promo_directory


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backfill_used (
  source_chat_id INTEGER NOT NULL,
  source_message_id INTEGER NOT NULL,
  used_at INTEGER NOT NULL,
  PRIMARY KEY (source_chat_id, source_message_id)
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


def _connect_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    return conn


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





def _mark_used(conn: sqlite3.Connection, source_chat_id: int, source_message_ids: list[int]) -> None:
    now_ts = int(time.time())
    for mid in source_message_ids:
        conn.execute(
            "INSERT OR REPLACE INTO backfill_used(source_chat_id, source_message_id, used_at) VALUES(?,?,?)",
            (int(source_chat_id), int(mid), int(now_ts)),
        )
    conn.commit()


def _is_used(conn: sqlite3.Connection, source_chat_id: int, source_message_ids: list[int]) -> bool:
    for mid in source_message_ids:
        row = conn.execute(
            "SELECT 1 FROM backfill_used WHERE source_chat_id=? AND source_message_id=?",
            (int(source_chat_id), int(mid)),
        ).fetchone()
        if row:
            return True
    return False


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


async def _forward_or_reupload_group(
    client: TelegramClient,
    *,
    target_entity,
    source_chat_id: int,
    msgs: list,
    dry_run: bool,
) -> None:
    msg_ids = [int(m.id) for m in msgs]

    if dry_run:
        print(f"DRY_RUN relay: source_chat_id={int(source_chat_id)} msg_ids={msg_ids} -> {getattr(target_entity, 'username', None) or target_entity}")
        return

    try:
        await client.forward_messages(target_entity, msg_ids, from_peer=int(source_chat_id))
        return
    except ChatForwardsRestrictedError:
        pass

    tmpdir = tempfile.mkdtemp(prefix="backfill_relay_")
    try:
        media_items = []
        for m in msgs:
            media = _media_for_send(m)
            if media is not None:
                media_items.append(media)

        paths: list[str] = []
        for item in media_items:
            p = await client.download_media(item, file=tmpdir)
            if p:
                paths.append(p)

        if not paths:
            return

        marker = f"[[SRC:{int(source_chat_id)}]]"

        if len(paths) == 1:
            await client.send_file(target_entity, paths[0], caption=str(marker), force_document=False)
        else:
            await client.send_file(target_entity, paths, caption=str(marker), force_document=False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def backfill_source(
    client: TelegramClient,
    conn: sqlite3.Connection,
    *,
    target_entity,
    source_chat_id: int,
    relay_cfg: dict,
    scan_limit: int,
    max_posts: int,
    sleep_between_posts: float,
    since_dt: datetime | None,
    dry_run: bool,
) -> int:
    posted = 0

    async for group in _iter_source_groups(client, source_chat_id, limit=scan_limit):
        msgs = sorted(group, key=lambda x: int(x.id))
        msg_ids = [int(m.id) for m in msgs]

        if since_dt is not None:
            newest = max((m.date for m in msgs if getattr(m, "date", None) is not None), default=None)
            if newest is not None and newest < since_dt:
                return posted

        if _is_used(conn, source_chat_id, msg_ids):
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

        try:
            await _forward_or_reupload_group(
                client,
                target_entity=target_entity,
                source_chat_id=int(source_chat_id),
                msgs=msgs,
                dry_run=bool(dry_run),
            )
        except FloodWaitError as e:
            await asyncio.sleep(int(e.seconds) + 1)
            await _forward_or_reupload_group(
                client,
                target_entity=target_entity,
                source_chat_id=int(source_chat_id),
                msgs=msgs,
                dry_run=bool(dry_run),
            )

        _mark_used(conn, source_chat_id, msg_ids)
        posted += 1

        if posted >= int(max_posts):
            return posted

        await asyncio.sleep(float(sleep_between_posts))

    return posted


async def main():
    parser = argparse.ArgumentParser(description="Backfill past posts by relaying them into the middle relay bot (so destinations are posted by the bot)")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--db", default="data/backfill_to_relaybot.sqlite3", help="SQLite db path for backfill tracking")

    parser.add_argument("--source-chat-id", type=int, action="append", help="Only backfill these source chats (can repeat)")

    parser.add_argument("--since-days", type=int, default=90, help="Only backfill messages newer than N days ago (default: 90)")
    parser.add_argument("--since-date", help="Only backfill messages newer than this date/time (ISO, e.g. 2026-01-01 or 2026-01-01T12:00:00Z)")

    parser.add_argument("--scan-limit", type=int, default=200000, help="How many recent source messages to scan per source")
    parser.add_argument("--max-posts", type=int, default=200000, help="Max posts (albums/singles) to send per source")
    parser.add_argument("--sleep-between-posts", type=float, default=1.0, help="Delay between relayed posts")
    parser.add_argument("--dry-run", action="store_true", help="Do not send anything, only print what would be sent")

    args = parser.parse_args()

    since_dt: datetime | None = None
    if args.since_days is not None and args.since_date is not None:
        raise SystemExit("Use only one of --since-days or --since-date")

    if args.since_date is not None:
        raw = str(args.since_date).strip()
        raw = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        since_dt = dt.astimezone(timezone.utc)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(days=int(args.since_days or 90))

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    relay_cfg = cfg.get("relay") or {}
    bot_mappings = cfg.get("bot_mappings") or []
    if not bot_mappings:
        raise SystemExit("bot_mappings is empty")

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

    target_cache: dict[str, Any] = {}

    only_sources = [int(x) for x in (args.source_chat_id or [])]

    try:
        for m in bot_mappings:
            if not isinstance(m, dict):
                continue

            src = m.get("source_chat")
            target_bot = m.get("target_bot")
            if src is None or not target_bot:
                continue

            try:
                src_id = int(src)
            except Exception:  # noqa: BLE001
                continue

            if only_sources and not any(int(src_id) == int(x) for x in only_sources):
                continue

            target_key = str(target_bot)
            if target_key not in target_cache:
                target_cache[target_key] = await client.get_entity(target_key)

            target_entity = target_cache[target_key]

            posted = await backfill_source(
                client,
                conn,
                target_entity=target_entity,
                source_chat_id=int(src_id),
                relay_cfg=relay_cfg,
                scan_limit=int(args.scan_limit),
                max_posts=int(args.max_posts),
                sleep_between_posts=float(args.sleep_between_posts),
                since_dt=since_dt,
                dry_run=bool(args.dry_run),
            )
            print(f"backfill_done: source_chat_id={int(src_id)} posted={int(posted)}")
    finally:
        conn.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
