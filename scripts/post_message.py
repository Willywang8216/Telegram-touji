import argparse
import asyncio
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions

from common_config import ConfigManager, load_userbot_settings


async def _get_topic_top_message(client: TelegramClient, peer, title: str) -> int:
    entity = await client.get_entity(peer)
    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q="",
        )
    )
    for t in res.topics:
        if t.title == title:
            return int(t.top_message)
    raise ValueError(f"Topic not found: {title}")


async def main():
    parser = argparse.ArgumentParser(description="Post a message using userbot (optionally into a forum topic)")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--peer", required=True, help="Peer ID (e.g. -100...) or @username")
    parser.add_argument("--topic-title", help="Forum topic title (exact match)")
    parser.add_argument("--reply-to", type=int, help="Reply-to message id (overrides --topic-title)")
    parser.add_argument("--text", required=True, help="Message text")
    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    settings = load_userbot_settings(config_manager)

    try:
        peer = int(args.peer)
    except ValueError:
        peer = args.peer

    client = TelegramClient("userbot_post", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    try:
        reply_to = args.reply_to
        if reply_to is None and args.topic_title:
            reply_to = await _get_topic_top_message(client, peer, args.topic_title)

        await client.send_message(peer, args.text, reply_to=reply_to)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
