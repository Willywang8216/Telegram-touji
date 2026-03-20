import argparse
import asyncio
import json
from datetime import datetime, timezone

from telethon import TelegramClient

from common_config import ConfigManager, load_userbot_settings


def _entity_type(entity) -> str:
    # telethon.tl.types.User/Chat/Channel
    name = entity.__class__.__name__
    if name == "User":
        return "user"
    if name == "Chat":
        return "chat"
    if name == "Channel":
        if getattr(entity, "broadcast", False):
            return "channel"
        if getattr(entity, "megagroup", False):
            return "supergroup"
        return "channel"
    return name


async def main() -> None:
    p = argparse.ArgumentParser(description="List dialogs (groups/channels/users) for the userbot account")
    p.add_argument(
        "--type",
        dest="types",
        action="append",
        default=[],
        help="Filter by type: user|chat|supergroup|channel (can be repeated)",
    )
    args = p.parse_args()
    type_filter = set(args.types or [])

    cm = ConfigManager()
    s = load_userbot_settings(cm)

    client = TelegramClient("anon", s["api_id"], s["api_hash"], proxy=s.get("proxy"))
    await client.start()

    # JSON Lines output so it's easy to grep/filter without breaking on special chars.
    async for d in client.iter_dialogs():
        e = d.entity
        t = _entity_type(e)
        if type_filter and t not in type_filter:
            continue

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "peer_id": int(d.id),
            "type": t,
            "title": getattr(e, "title", None) or getattr(e, "first_name", None) or "",
            "username": getattr(e, "username", None),
            "is_forum": bool(getattr(e, "forum", False)),
        }
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
