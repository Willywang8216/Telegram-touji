import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, types

from common_config import ConfigManager, load_userbot_settings


async def main():
    parser = argparse.ArgumentParser(description="List default forum topic icon emoji -> custom emoji document id")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    settings = load_userbot_settings(config_manager)

    session_dir = Path("data/sessions")
    session_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_dir / "userbot_tools"), settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    try:
        res = await client(
            functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetEmojiDefaultTopicIcons(),
                hash=0,
            )
        )

        out = []
        for doc in getattr(res, "documents", []) or []:
            alt = None
            for attr in getattr(doc, "attributes", []) or []:
                if isinstance(attr, types.DocumentAttributeCustomEmoji):
                    alt = getattr(attr, "alt", None)
                    break
            if not alt:
                continue
            out.append({"emoji": str(alt), "icon_emoji_id": int(doc.id)})

        out.sort(key=lambda x: x["emoji"])
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
