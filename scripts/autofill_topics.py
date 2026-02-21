import argparse
import asyncio
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from common_config import ConfigManager, load_userbot_settings


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidates (
  chat_id INTEGER NOT NULL,
  top_message INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  has_media INTEGER NOT NULL,
  msg_date INTEGER,
  last_used INTEGER,
  PRIMARY KEY (chat_id, top_message, message_id)
);

CREATE TABLE IF NOT EXISTS topic_scan (
  chat_id INTEGER NOT NULL,
  top_message INTEGER NOT NULL,
  offset_id INTEGER NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chat_id, top_message)
);
"""


@dataclass(frozen=True)
class TopicRef:
    chat_id: int
    title: str
    top_message: int


def _connect_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    return conn


def _local_day_start_utc(tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


async def _count_today_messages(client: TelegramClient, topic: TopicRef, day_start_utc: datetime, need: int) -> int:
    # We only need to know whether we have >= need messages.
    count = 0
    async for m in client.iter_messages(topic.chat_id, reply_to=topic.top_message, limit=max(need * 3, 50)):
        if not getattr(m, "date", None):
            continue
        if m.date < day_start_utc:
            break
        if m.id == topic.top_message:
            continue
        # Count anything with media; text-only posts are less useful for this bot.
        if m.media is None:
            continue
        count += 1
        if count >= need:
            return count
    return count


def _get_scan_state(conn: sqlite3.Connection, topic: TopicRef) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT offset_id, done FROM topic_scan WHERE chat_id=? AND top_message=?",
        (topic.chat_id, topic.top_message),
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT OR REPLACE INTO topic_scan(chat_id, top_message, offset_id, done) VALUES(?,?,?,?)",
            (topic.chat_id, topic.top_message, 0, 0),
        )
        conn.commit()
        return 0, False
    return int(row[0] or 0), bool(row[1])


def _set_scan_state(conn: sqlite3.Connection, topic: TopicRef, offset_id: int, done: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO topic_scan(chat_id, top_message, offset_id, done) VALUES(?,?,?,?)",
        (topic.chat_id, topic.top_message, int(offset_id), 1 if done else 0),
    )
    conn.commit()


async def _scan_candidates(
    client: TelegramClient,
    conn: sqlite3.Connection,
    topic: TopicRef,
    batch_size: int,
    max_total: int,
) -> None:
    offset_id, done = _get_scan_state(conn, topic)
    if done:
        return

    existing_total = conn.execute(
        "SELECT COUNT(1) FROM candidates WHERE chat_id=? AND top_message=?",
        (topic.chat_id, topic.top_message),
    ).fetchone()[0]
    if existing_total >= max_total:
        _set_scan_state(conn, topic, offset_id=offset_id, done=True)
        return

    limit = min(batch_size, max_total - int(existing_total))
    if limit <= 0:
        _set_scan_state(conn, topic, offset_id=offset_id, done=True)
        return

    min_id = None
    inserted = 0

    async for m in client.iter_messages(topic.chat_id, reply_to=topic.top_message, limit=limit, offset_id=offset_id):
        if m.id == topic.top_message:
            continue
        if m.media is None:
            continue

        msg_date = int(m.date.timestamp()) if getattr(m, "date", None) else None
        conn.execute(
            "INSERT OR IGNORE INTO candidates(chat_id, top_message, message_id, has_media, msg_date, last_used) VALUES(?,?,?,?,?,?)",
            (topic.chat_id, topic.top_message, int(m.id), 1, msg_date, None),
        )
        inserted += 1
        if min_id is None or m.id < min_id:
            min_id = int(m.id)

    conn.commit()

    # If we didn't insert anything and we didn't advance, consider the scan complete.
    if min_id is None:
        _set_scan_state(conn, topic, offset_id=offset_id, done=True)
        return

    # Advance the scan to older messages.
    new_offset = int(min_id)
    if new_offset == offset_id:
        _set_scan_state(conn, topic, offset_id=new_offset, done=True)
    else:
        _set_scan_state(conn, topic, offset_id=new_offset, done=False)


def _pick_candidate(conn: sqlite3.Connection, topic: TopicRef, now_ts: int, lookback_days: int) -> int | None:
    cutoff = now_ts - (lookback_days * 86400)

    row = conn.execute(
        """
        SELECT message_id
        FROM candidates
        WHERE chat_id=? AND top_message=? AND has_media=1
          AND (last_used IS NULL OR last_used < ?)
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (topic.chat_id, topic.top_message, cutoff),
    ).fetchone()

    if row:
        return int(row[0])

    row = conn.execute(
        """
        SELECT message_id
        FROM candidates
        WHERE chat_id=? AND top_message=? AND has_media=1
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (topic.chat_id, topic.top_message),
    ).fetchone()

    if not row:
        return None
    return int(row[0])


async def _repost_message(client: TelegramClient, topic: TopicRef, message_id: int) -> None:
    msg = await client.get_messages(topic.chat_id, ids=message_id)
    if not msg or msg.media is None:
        return

    await client.send_message(topic.chat_id, message="", file=msg.media, reply_to=topic.top_message)


def _mark_used(conn: sqlite3.Connection, topic: TopicRef, message_id: int, now_ts: int) -> None:
    conn.execute(
        "UPDATE candidates SET last_used=? WHERE chat_id=? AND top_message=? AND message_id=?",
        (now_ts, topic.chat_id, topic.top_message, int(message_id)),
    )
    conn.commit()


def _load_topics_from_config(cfg: dict) -> list[TopicRef]:
    relay = cfg.get("relay") or {}
    mapping = relay.get("forum_topic_top_messages") or {}

    out: list[TopicRef] = []
    for chat_key, topics in mapping.items():
        try:
            chat_id = int(chat_key)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(topics, dict):
            continue
        for title, top_message in topics.items():
            try:
                out.append(TopicRef(chat_id=chat_id, title=str(title), top_message=int(top_message)))
            except Exception:  # noqa: BLE001
                continue
    return out


async def run_once(
    client: TelegramClient,
    conn: sqlite3.Connection,
    topics: list[TopicRef],
    min_posts_per_topic_per_day: int,
    tz_name: str,
    lookback_days: int,
    scan_batch: int,
    max_scan_per_topic: int,
    max_posts_per_run: int,
    sleep_between_posts: float,
) -> None:
    day_start_utc = _local_day_start_utc(tz_name)
    now_ts = int(time.time())

    posted = 0

    for topic in topics:
        have = await _count_today_messages(client, topic, day_start_utc=day_start_utc, need=min_posts_per_topic_per_day)
        missing = max(0, int(min_posts_per_topic_per_day) - int(have))
        if missing <= 0:
            continue

        # Refresh candidate pool (incremental scan).
        await _scan_candidates(client, conn, topic, batch_size=scan_batch, max_total=max_scan_per_topic)

        for _ in range(missing):
            if posted >= max_posts_per_run:
                return

            pick = _pick_candidate(conn, topic, now_ts=now_ts, lookback_days=lookback_days)
            if pick is None:
                break

            try:
                await _repost_message(client, topic, pick)
            except FloodWaitError as e:
                await asyncio.sleep(int(e.seconds) + 1)
                await _repost_message(client, topic, pick)

            _mark_used(conn, topic, pick, now_ts=now_ts)
            posted += 1
            await asyncio.sleep(float(sleep_between_posts))


async def main():
    parser = argparse.ArgumentParser(description="Autofill forum topics with reposts to reach a daily minimum")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--db", default="data/autofill.sqlite3", help="SQLite db path for repost tracking")
    parser.add_argument("--min-per-topic", type=int, default=10, help="Minimum posts per topic per day")
    parser.add_argument("--tz", default=os.getenv("TZ", "Asia/Taipei"), help="Timezone for daily counting")
    parser.add_argument("--lookback-days", type=int, default=30, help="Avoid reposting the same message within N days")
    parser.add_argument("--scan-batch", type=int, default=300, help="How many messages to scan per topic per run")
    parser.add_argument("--max-scan-per-topic", type=int, default=5000, help="Max messages to remember per topic")
    parser.add_argument("--max-posts-per-run", type=int, default=200, help="Safety cap")
    parser.add_argument("--sleep-between-posts", type=float, default=1.0, help="Delay between posts")
    parser.add_argument("--daemon", action="store_true", help="Run forever")
    parser.add_argument("--interval-min", type=int, default=60, help="Loop interval in minutes for --daemon")
    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    topics = _load_topics_from_config(cfg)
    if not topics:
        raise SystemExit("relay.forum_topic_top_messages is empty; run scripts/sync_forum_topics.py --write first")

    settings = load_userbot_settings(config_manager)
    client = TelegramClient("autofill_session", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    conn = _connect_db(args.db)

    try:
        while True:
            await run_once(
                client,
                conn,
                topics,
                min_posts_per_topic_per_day=args.min_per_topic,
                tz_name=args.tz,
                lookback_days=args.lookback_days,
                scan_batch=args.scan_batch,
                max_scan_per_topic=args.max_scan_per_topic,
                max_posts_per_run=args.max_posts_per_run,
                sleep_between_posts=args.sleep_between_posts,
            )

            if not args.daemon:
                return
            await asyncio.sleep(int(args.interval_min) * 60)
    finally:
        conn.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
