import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions

from common_config import ConfigManager, load_userbot_settings


async def list_topics(client: TelegramClient, peer):
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
    return entity, {t.title: t.top_message for t in res.topics}


async def ensure_topics(client: TelegramClient, peer, titles: list[str]):
    entity, existing = await list_topics(client, peer)
    missing = [t for t in titles if t not in existing]

    for title in missing:
        await client(functions.messages.CreateForumTopicRequest(peer=entity, title=title, icon_color=0x6FB9F0))

    if missing:
        _, existing = await list_topics(client, peer)

    return existing


async def main():
    parser = argparse.ArgumentParser(description="Ensure forum topics exist and write topic->top_message mapping into config")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--write", action="store_true", help="Write mapping into relay.forum_topic_top_messages")
    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    relay = cfg.get("relay") or {}
    ensure = relay.get("ensure_forum_topics") or []
    if not ensure:
        raise SystemExit("relay.ensure_forum_topics is empty")

    settings = load_userbot_settings(config_manager)
    client = TelegramClient("topics_session", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    try:
        out: dict[str, dict[str, int]] = {}
        for item in ensure:
            chat_id = item.get("chat_id")
            titles = [str(t) for t in (item.get("topics") or [])]
            if not chat_id or not titles:
                continue

            existing = await ensure_topics(client, int(chat_id), titles)
            out[str(int(chat_id))] = {t: int(existing[t]) for t in titles if t in existing}

        print(json.dumps(out, ensure_ascii=False, indent=2))

        if args.write:
            relay["forum_topic_top_messages"] = out
            cfg["relay"] = relay
            config_manager.save(cfg)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
