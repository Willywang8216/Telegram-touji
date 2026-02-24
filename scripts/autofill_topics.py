import argparse
import asyncio
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError
from telethon.errors.rpcbaseerrors import BadRequestError
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError

from common_config import ConfigManager, load_userbot_settings
from message_filter import should_block_text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidates2 (
  chat_id INTEGER NOT NULL,
  topic_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  has_media INTEGER NOT NULL,
  msg_date INTEGER,
  last_used INTEGER,
  PRIMARY KEY (chat_id, topic_id, message_id)
);

CREATE TABLE IF NOT EXISTS topic_scan2 (
  chat_id INTEGER NOT NULL,
  topic_id INTEGER NOT NULL,
  offset_id INTEGER NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chat_id, topic_id)
);
"""


AUTOFILL_BUILTIN_BLOCKLIST_SUBSTRINGS = [
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
    "毒品",
    "大麻",
    "可卡因",
    "冰毒",
    "摇头丸",
    "搖頭丸",
    "k粉",
    "mdma",
    "ketamine",
]


class TopicInvalidError(Exception):
    pass


@dataclass
class TopicRef:
    chat_id: int
    title: str
    topic_id: int
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


def _is_topic_id_invalid(exc: Exception) -> bool:
    return "topic_id_invalid" in str(exc).lower()


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


async def _resolve_topic(client: TelegramClient, chat_id: int, title: str) -> tuple[int, int] | None:
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

    return None


def _raw_message_text(msg) -> str:
    return (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()


def _media_for_repost(msg):
    if getattr(msg, "sticker", None) is not None:
        return None

    media = getattr(msg, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
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


def _build_filter_settings(cfg: dict) -> dict:
    relay = cfg.get("relay") or {}

    blocklist_substrings = AUTOFILL_BUILTIN_BLOCKLIST_SUBSTRINGS + list(relay.get("autofill_blocklist_substrings") or [])

    block_contact_ads = relay.get("autofill_block_contact_ads")
    if block_contact_ads is None:
        # Autofill usually reposts without the original caption, so default to False.
        block_contact_ads = False

    return {
        "blocklist_substrings": blocklist_substrings,
        "blocklist_regexes": relay.get("autofill_blocklist_regexes") or [],
        "block_contact_ads": bool(block_contact_ads),
        "contact_ad_keywords": relay.get("autofill_contact_ad_keywords") or relay.get("contact_ad_keywords"),
    }


def _should_skip_message(msg, filter_settings: dict) -> bool:
    if _media_for_repost(msg) is None:
        return True

    text = _raw_message_text(msg)
    if text and should_block_text(
        text,
        blocklist_substrings=filter_settings.get("blocklist_substrings"),
        blocklist_regexes=filter_settings.get("blocklist_regexes"),
        block_contact_ads=bool(filter_settings.get("block_contact_ads", True)),
        contact_ad_keywords=filter_settings.get("contact_ad_keywords"),
    ):
        return True

    return False


async def _iter_topic_messages(client: TelegramClient, topic: TopicRef, *, limit: int, offset_id: int = 0):
    reply_to_candidates = [int(topic.topic_id), int(topic.top_message)]
    last_exc = None

    for reply_to in reply_to_candidates:
        if reply_to <= 0:
            continue
        try:
            it = client.iter_messages(
                topic.chat_id,
                reply_to=int(reply_to),
                limit=int(limit),
                offset_id=int(offset_id),
            )

            try:
                first = await it.__anext__()
            except StopAsyncIteration:
                continue

            yield first

            while True:
                try:
                    m = await it.__anext__()
                except StopAsyncIteration:
                    break
                yield m

            return
        except BadRequestError as exc:
            if _is_topic_id_invalid(exc):
                last_exc = exc
                continue
            raise

    raise TopicInvalidError(str(last_exc) if last_exc else "TOPIC_ID_INVALID")


async def _count_today_messages(
    client: TelegramClient,
    topic: TopicRef,
    day_start_utc: datetime,
    need: int,
    filter_settings: dict,
) -> int:
    count = 0
    async for m in _iter_topic_messages(client, topic, limit=max(int(need) * 3, 50)):
        if not getattr(m, "date", None):
            continue
        if m.date < day_start_utc:
            break
        if _should_skip_message(m, filter_settings):
            continue
        count += 1
        if count >= need:
            return count
    return count


def _topic_key(topic: TopicRef) -> int:
    return int(topic.topic_id)


def _get_scan_state(conn: sqlite3.Connection, topic: TopicRef) -> tuple[int, bool]:
    topic_key = _topic_key(topic)
    row = conn.execute(
        "SELECT offset_id, done FROM topic_scan2 WHERE chat_id=? AND topic_id=?",
        (topic.chat_id, topic_key),
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT OR REPLACE INTO topic_scan2(chat_id, topic_id, offset_id, done) VALUES(?,?,?,?)",
            (topic.chat_id, topic_key, 0, 0),
        )
        conn.commit()
        return 0, False
    return int(row[0] or 0), bool(row[1])


def _set_scan_state(conn: sqlite3.Connection, topic: TopicRef, offset_id: int, done: bool) -> None:
    topic_key = _topic_key(topic)
    conn.execute(
        "INSERT OR REPLACE INTO topic_scan2(chat_id, topic_id, offset_id, done) VALUES(?,?,?,?)",
        (topic.chat_id, topic_key, int(offset_id), 1 if done else 0),
    )
    conn.commit()


async def _scan_candidates(
    client: TelegramClient,
    conn: sqlite3.Connection,
    topic: TopicRef,
    batch_size: int,
    max_total: int,
    filter_settings: dict,
    self_user_id: int | None,
    *,
    debug: bool = False,
    debug_limit: int = 20,
) -> None:
    offset_id, done = _get_scan_state(conn, topic)
    topic_key = _topic_key(topic)

    existing_total = conn.execute(
        "SELECT COUNT(1) FROM candidates2 WHERE chat_id=? AND topic_id=? AND has_media=1",
        (topic.chat_id, topic_key),
    ).fetchone()[0]

    if done and int(existing_total) <= 0:
        offset_id = 0
        done = False
        _set_scan_state(conn, topic, offset_id=0, done=False)

    if done:
        return

    if int(existing_total) >= int(max_total):
        _set_scan_state(conn, topic, offset_id=offset_id, done=True)
        return

    limit = min(int(batch_size), int(max_total) - int(existing_total))
    if limit <= 0:
        _set_scan_state(conn, topic, offset_id=offset_id, done=True)
        return

    min_seen_id = None

    scanned_total = 0
    skipped_self = 0
    skipped_no_media = 0
    skipped_blocked = 0
    inserted = 0
    debug_printed = 0

    def _debug(reason: str, m) -> None:
        nonlocal debug_printed
        if not debug or debug_printed >= int(debug_limit):
            return

        media = getattr(m, "media", None)
        media_type = type(media).__name__ if media is not None else None
        doc = getattr(m, "document", None)
        mime = (getattr(doc, "mime_type", None) or "").lower() if doc is not None else None

        text = _raw_message_text(m)
        text = (text[:120] + "…") if len(text) > 120 else text

        print(
            f"scan_skip: reason={reason} msg_id={getattr(m, 'id', None)} media={media_type} mime={mime!r} text={text!r}"
        )
        debug_printed += 1

    async for m in _iter_topic_messages(client, topic, limit=limit, offset_id=offset_id):
        scanned_total += 1

        if min_seen_id is None or int(m.id) < int(min_seen_id):
            min_seen_id = int(m.id)

        if self_user_id is not None and getattr(m, "sender_id", None) == int(self_user_id):
            skipped_self += 1
            _debug("self", m)
            continue

        if _media_for_repost(m) is None:
            skipped_no_media += 1
            _debug("no_media", m)
            continue

        text = _raw_message_text(m)
        if text and should_block_text(
            text,
            blocklist_substrings=filter_settings.get("blocklist_substrings"),
            blocklist_regexes=filter_settings.get("blocklist_regexes"),
            block_contact_ads=bool(filter_settings.get("block_contact_ads", True)),
            contact_ad_keywords=filter_settings.get("contact_ad_keywords"),
        ):
            skipped_blocked += 1
            _debug("blocked_text", m)
            continue

        msg_date = int(m.date.timestamp()) if getattr(m, "date", None) else None
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO candidates2(chat_id, topic_id, message_id, has_media, msg_date, last_used) VALUES(?,?,?,?,?,?)",
            (topic.chat_id, topic_key, int(m.id), 1, msg_date, None),
        )
        if conn.total_changes != before:
            inserted += 1

    conn.commit()

    if debug:
        print(
            f"scan_stats: chat_id={topic.chat_id} title={topic.title!r} scanned={scanned_total} inserted={inserted} skipped_self={skipped_self} skipped_no_media={skipped_no_media} skipped_blocked={skipped_blocked}"
        )

    if min_seen_id is None:
        _set_scan_state(conn, topic, offset_id=offset_id, done=True)
        return

    new_offset = int(min_seen_id)
    if new_offset == int(offset_id):
        _set_scan_state(conn, topic, offset_id=new_offset, done=True)
    else:
        _set_scan_state(conn, topic, offset_id=new_offset, done=False)


def _pick_candidate(conn: sqlite3.Connection, topic: TopicRef, now_ts: int, lookback_days: int) -> int | None:
    cutoff = int(now_ts) - (int(lookback_days) * 86400)
    topic_key = _topic_key(topic)

    row = conn.execute(
        """
        SELECT message_id
        FROM candidates2
        WHERE chat_id=? AND topic_id=? AND has_media=1
          AND (last_used IS NULL OR last_used < ?)
        ORDER BY RANDOM()
        LIMIT 1
        """,
        (topic.chat_id, topic_key, cutoff),
    ).fetchone()

    if row:
        return int(row[0])

    row = conn.execute(
        """
        SELECT message_id
        FROM candidates2
        WHERE chat_id=? AND topic_id=? AND has_media=1
        ORDER BY (last_used IS NULL) DESC, last_used ASC, RANDOM()
        LIMIT 1
        """,
        (topic.chat_id, topic_key),
    ).fetchone()

    if not row:
        return None
    return int(row[0])


def _pick_autofill_caption(cfg: dict, chat_id: int) -> str:
    relay = cfg.get("relay") or {}

    captions = relay.get("autofill_captions")
    if captions is None:
        captions = relay.get("post_captions")

    if not captions:
        return ""

    value = captions
    if isinstance(captions, dict):
        value = captions.get(str(chat_id))

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        items = [str(x) for x in value if x]
        if not items:
            return ""
        return random.choice(items)

    return ""


async def _repost_message(
    client: TelegramClient,
    topic: TopicRef,
    message_id: int,
    filter_settings: dict,
    caption: str,
) -> bool:
    msg = await client.get_messages(topic.chat_id, ids=message_id)
    if not msg:
        return False

    if _should_skip_message(msg, filter_settings):
        return False

    media = _media_for_repost(msg)
    if media is None:
        return False

    caption = str(caption or "")

    try:
        await client.send_file(topic.chat_id, media, caption=caption, reply_to=topic.top_message)
        return True
    except ChatForwardsRestrictedError:
        tmpdir = tempfile.mkdtemp(prefix="autofill_")
        try:
            path = await client.download_media(msg, file=tmpdir)
            if not path:
                return False
            await client.send_file(topic.chat_id, path, caption=caption, reply_to=topic.top_message)
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _mark_used(conn: sqlite3.Connection, topic: TopicRef, message_id: int, now_ts: int) -> None:
    topic_key = _topic_key(topic)
    conn.execute(
        "UPDATE candidates2 SET last_used=? WHERE chat_id=? AND topic_id=? AND message_id=?",
        (int(now_ts), topic.chat_id, topic_key, int(message_id)),
    )
    conn.commit()


def _load_topics_from_config(cfg: dict) -> list[TopicRef]:
    relay = cfg.get("relay") or {}

    ensure = relay.get("ensure_forum_topics") or []
    if isinstance(ensure, list) and ensure:
        out: list[TopicRef] = []
        for item in ensure:
            chat_id = item.get("chat_id")
            titles = item.get("topics") or []
            if not chat_id or not isinstance(titles, list):
                continue
            try:
                chat_id_int = int(chat_id)
            except Exception:  # noqa: BLE001
                continue
            for title in titles:
                if not title:
                    continue
                out.append(TopicRef(chat_id=chat_id_int, title=str(title), topic_id=0, top_message=0))
        if out:
            return out

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
                out.append(TopicRef(chat_id=chat_id, title=str(title), topic_id=0, top_message=int(top_message)))
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
    filter_settings: dict,
    inactive_min: int,
    fill_count: int,
    top_up_window: bool,
    force: bool,
    self_user_id: int | None,
    cfg: dict,
    *,
    debug_scan: bool = False,
    debug_scan_limit: int = 20,
) -> None:
    now_utc = datetime.now(timezone.utc)

    if int(inactive_min) > 0:
        day_start_utc = now_utc - timedelta(minutes=int(inactive_min))
    else:
        day_start_utc = _local_day_start_utc(tz_name)

    now_ts = int(time.time())

    posted = 0

    for topic in topics:
        if topic.topic_id <= 0 or topic.top_message <= 0:
            resolved = await _resolve_topic(client, topic.chat_id, topic.title)
            if not resolved:
                print(f"skip topic (not found): chat_id={topic.chat_id} title={topic.title!r}")
                continue
            topic.topic_id, topic.top_message = int(resolved[0]), int(resolved[1])

        mode_inactive = int(inactive_min) > 0
        count_need = 1
        if force:
            count_need = 1
        elif mode_inactive and bool(top_up_window):
            count_need = int(fill_count)
        elif not mode_inactive:
            count_need = int(min_posts_per_topic_per_day)

        have = None
        for _ in range(2):
            try:
                have = await _count_today_messages(
                    client,
                    topic,
                    day_start_utc=day_start_utc,
                    need=int(count_need),
                    filter_settings=filter_settings,
                )
                break
            except TopicInvalidError:
                resolved = await _resolve_topic(client, topic.chat_id, topic.title)
                if not resolved:
                    have = None
                    break
                topic.topic_id, topic.top_message = int(resolved[0]), int(resolved[1])

        if have is None:
            print(f"skip topic (TOPIC_ID_INVALID): chat_id={topic.chat_id} title={topic.title!r}")
            continue

        if bool(mode_inactive) and not bool(force) and not bool(top_up_window):
            if int(have) > 0:
                continue
            missing = int(fill_count)
        elif bool(mode_inactive) and bool(force):
            missing = int(fill_count)
        elif bool(mode_inactive) and bool(top_up_window):
            missing = max(0, int(fill_count) - int(have))
        else:
            missing = max(0, int(min_posts_per_topic_per_day) - int(have))

        if missing <= 0:
            continue

        scanned = False
        for _ in range(2):
            try:
                await _scan_candidates(
                    client,
                    conn,
                    topic,
                    batch_size=scan_batch,
                    max_total=max_scan_per_topic,
                    filter_settings=filter_settings,
                    self_user_id=self_user_id,
                    debug=bool(debug_scan),
                    debug_limit=int(debug_scan_limit),
                )
                scanned = True
                break
            except TopicInvalidError:
                resolved = await _resolve_topic(client, topic.chat_id, topic.title)
                if not resolved:
                    scanned = False
                    break
                topic.topic_id, topic.top_message = int(resolved[0]), int(resolved[1])

        if not scanned:
            print(f"skip topic (scan TOPIC_ID_INVALID): chat_id={topic.chat_id} title={topic.title!r}")
            continue

        topic_key = _topic_key(topic)
        candidate_total = conn.execute(
            "SELECT COUNT(1) FROM candidates2 WHERE chat_id=? AND topic_id=? AND has_media=1",
            (topic.chat_id, topic_key),
        ).fetchone()[0]

        if int(candidate_total) <= 0:
            print(f"skip topic (no candidates): chat_id={topic.chat_id} title={topic.title!r}")
            continue

        remaining = int(missing)
        attempts_left = max(remaining * 5, remaining)

        while remaining > 0 and attempts_left > 0:
            if posted >= max_posts_per_run:
                return

            pick = _pick_candidate(conn, topic, now_ts=now_ts, lookback_days=lookback_days)
            if pick is None:
                break

            caption = _pick_autofill_caption(cfg, topic.chat_id)

            ok = False
            try:
                ok = await _repost_message(client, topic, pick, filter_settings=filter_settings, caption=caption)
            except FloodWaitError as e:
                await asyncio.sleep(int(e.seconds) + 1)
                ok = await _repost_message(client, topic, pick, filter_settings=filter_settings, caption=caption)

            _mark_used(conn, topic, pick, now_ts=now_ts)
            attempts_left -= 1

            if not ok:
                continue

            posted += 1
            remaining -= 1
            await asyncio.sleep(float(sleep_between_posts))


async def main():
    parser = argparse.ArgumentParser(
        description="Autofill forum topics by reposting older media (daily minimum, or when a topic is inactive)"
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--db", default="data/autofill.sqlite3", help="SQLite db path for repost tracking")

    parser.add_argument(
        "--min-per-topic",
        type=int,
        default=10,
        help="Daily mode: minimum media posts per topic per day (ignored if --inactive-min > 0)",
    )
    parser.add_argument("--tz", default=os.getenv("TZ", "Asia/Taipei"), help="Timezone for daily counting")

    parser.add_argument(
        "--inactive-min",
        type=int,
        default=0,
        help="If > 0: treat the last N minutes as the activity window. Default behaviour posts when a topic had 0 posts in the window.",
    )
    parser.add_argument(
        "--fill-count",
        type=int,
        default=10,
        help="Inactive mode: how many posts to add when inactive (or target per window with --top-up-window)",
    )
    parser.add_argument(
        "--top-up-window",
        action="store_true",
        help="Inactive mode: top up to --fill-count posts within the window (instead of only if 0)",
    )
    parser.add_argument("--force", action="store_true", help="Ignore activity checks and always post in inactive mode")

    parser.add_argument("--only-chat-id", type=int, help="Only run topics in this chat_id")
    parser.add_argument("--only-topic-title", help="Only run a single topic title (exact match)")

    parser.add_argument("--lookback-days", type=int, default=30, help="Avoid reposting the same source message within N days")
    parser.add_argument("--scan-batch", type=int, default=300, help="How many messages to scan per topic per run")
    parser.add_argument("--max-scan-per-topic", type=int, default=5000, help="Max messages to remember per topic")
    parser.add_argument("--max-posts-per-run", type=int, default=200, help="Safety cap")
    parser.add_argument("--sleep-between-posts", type=float, default=1.0, help="Delay between posts")

    parser.add_argument("--debug-scan", action="store_true", help="Print why messages are skipped during scanning")
    parser.add_argument("--debug-scan-limit", type=int, default=20, help="Max per-topic scan_skip lines")

    parser.add_argument("--daemon", action="store_true", help="Run forever")
    parser.add_argument("--interval-min", type=int, default=60, help="Loop interval in minutes for --daemon")
    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    topics = _load_topics_from_config(cfg)
    if not topics:
        raise SystemExit("No topics configured: set relay.ensure_forum_topics or relay.forum_topic_top_messages")

    if args.only_chat_id is not None:
        topics = [t for t in topics if int(t.chat_id) == int(args.only_chat_id)]

    if args.only_topic_title:
        topics = [t for t in topics if t.title == str(args.only_topic_title)]

    if not topics:
        raise SystemExit("No matching topics after applying --only-* filters")

    filter_settings = _build_filter_settings(cfg)

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

    me = await client.get_me()
    self_user_id = int(me.id) if me else None

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
                filter_settings=filter_settings,
                inactive_min=args.inactive_min,
                fill_count=args.fill_count,
                top_up_window=bool(args.top_up_window),
                force=bool(args.force),
                self_user_id=self_user_id,
                cfg=cfg,
                debug_scan=bool(args.debug_scan),
                debug_scan_limit=int(args.debug_scan_limit),
            )

            if not args.daemon:
                return
            await asyncio.sleep(int(args.interval_min) * 60)
    finally:
        conn.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
