import argparse
import asyncio

from telethon import TelegramClient
from telethon.tl.functions.channels import GetForumTopicsRequest

from common_config import ConfigManager, load_userbot_settings


async def main():
    parser = argparse.ArgumentParser(description="List forum topics in a supergroup")
    parser.add_argument("--peer", required=True, help="Peer ID (e.g. -100...) or @username")
    args = parser.parse_args()

    config_manager = ConfigManager()
    settings = load_userbot_settings(config_manager)

    client = TelegramClient("topics_session", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    try:
        peer_raw = args.peer
        try:
            peer = int(peer_raw)
        except ValueError:
            peer = peer_raw

        entity = await client.get_entity(peer)
        res = await client(
            GetForumTopicsRequest(
                channel=entity,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100,
                q="",
            )
        )

        for i, t in enumerate(res.topics, start=1):
            print(f"[{i}] id={t.id}  title={t.title}  top_message={t.top_message}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
