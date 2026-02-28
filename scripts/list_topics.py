import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, utils

from common_config import ConfigManager, load_userbot_settings


def _is_forum_supergroup(entity) -> bool:
    # Forum topics are only available in supergroups (Channel.megagroup=True) with forum enabled.
    return bool(getattr(entity, "megagroup", False)) and bool(getattr(entity, "forum", False))


async def _get_forum_topics(client: TelegramClient, entity, *, limit: int = 100):
    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=int(limit),
            q="",
        )
    )
    return list(getattr(res, "topics", []) or [])


async def _print_one_forum(client: TelegramClient, entity, *, as_json: bool, topics_limit: int):
    topics = await _get_forum_topics(client, entity, limit=topics_limit)

    peer_id = utils.get_peer_id(entity)
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)
    username = getattr(entity, "username", None)

    if as_json:
        return {
            "peer_id": int(peer_id),
            "title": str(title),
            "username": f"@{username}" if username else None,
            "topics": {t.title: int(t.top_message) for t in topics},
        }

    header = f"=== {title} (peer_id={peer_id}{', @' + username if username else ''}) ==="
    print(header)
    if not topics:
        print("(no topics)")
        return None

    for i, t in enumerate(topics, start=1):
        print(f"[{i}] id={t.id}  title={t.title}  top_message={t.top_message}")
    print()
    return None


async def main():
    parser = argparse.ArgumentParser(
        description="List forum topics in a supergroup, or list all forum-enabled supergroups and their topics"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--peer", help="Peer ID (e.g. -100...) or @username")
    group.add_argument(
        "--all-forums",
        action="store_true",
        help="List all forum-enabled supergroups that this userbot account can see (from dialogs)",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="--peer: output {title: top_message}. --all-forums: output a JSON list of {peer_id,title,username,topics}",
    )

    parser.add_argument("--topics-limit", type=int, default=100, help="Max topics to fetch per forum (default: 100)")
    parser.add_argument(
        "--dialogs-limit",
        type=int,
        default=0,
        help="When using --all-forums, limit how many dialogs to scan (0 = no limit)",
    )

    args = parser.parse_args()

    config_manager = ConfigManager()
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

    try:
        if args.all_forums:
            out = []
            scanned = 0

            async for d in client.iter_dialogs():
                if args.dialogs_limit and scanned >= int(args.dialogs_limit):
                    break
                scanned += 1

                ent = getattr(d, "entity", None)
                if ent is None:
                    continue

                if not _is_forum_supergroup(ent):
                    continue

                try:
                    item = await _print_one_forum(client, ent, as_json=bool(args.json), topics_limit=int(args.topics_limit))
                    if args.json and item:
                        out.append(item)
                except Exception as exc:  # noqa: BLE001
                    # Keep going; a single bad dialog shouldn't break the listing.
                    peer_id = None
                    try:
                        peer_id = utils.get_peer_id(ent)
                    except Exception:  # noqa: BLE001
                        pass
                    if args.json:
                        out.append({"peer_id": peer_id, "error": str(exc)})
                    else:
                        print(f"[WARN] failed to list topics: peer_id={peer_id} error={exc}")

            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        # --peer mode
        peer_raw = args.peer
        try:
            peer = int(peer_raw)
        except ValueError:
            peer = peer_raw

        entity = await client.get_entity(peer)
        topics = await _get_forum_topics(client, entity, limit=int(args.topics_limit))

        if args.json:
            mapping = {t.title: int(t.top_message) for t in topics}
            print(json.dumps(mapping, ensure_ascii=False, indent=2))
            return

        for i, t in enumerate(topics, start=1):
            print(f"[{i}] id={t.id}  title={t.title}  top_message={t.top_message}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
