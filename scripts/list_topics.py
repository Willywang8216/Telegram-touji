import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions

from common_config import ConfigManager, load_userbot_settings


async def main():
    parser = argparse.ArgumentParser(description="List forum topics in a supergroup")
    parser.add_argument("--peer", required=True, help="Peer ID (e.g. -100...) or @username")
    parser.add_argument("--json", action="store_true", help="Output {title: top_message} as JSON")
    args = parser.parse_args()

    config_manager = ConfigManager()
    settings = load_userbot_settings(config_manager)

    session_dir = Path("data/sessions")
    session_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_dir / "userbot_tools"), settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    try:
        peer_raw = args.peer
        try:
            peer = int(peer_raw)
        except ValueError:
            peer = peer_raw

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

        if args.json:
            mapping = {t.title: int(t.top_message) for t in res.topics}
            print(json.dumps(mapping, ensure_ascii=False, indent=2))
            return

        for i, t in enumerate(res.topics, start=1):
            print(f"[{i}] id={t.id}  title={t.title}  top_message={t.top_message}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
