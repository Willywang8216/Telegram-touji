import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, types, utils

from common_config import ConfigManager, load_userbot_settings


def _entity_kind(ent) -> str:
    if isinstance(ent, types.User):
        return "user"
    if isinstance(ent, types.Chat):
        return "group"
    if isinstance(ent, types.Channel):
        # In Telegram API, supergroups and channels are both Channel.
        if bool(getattr(ent, "megagroup", False)):
            return "supergroup"
        if bool(getattr(ent, "broadcast", False)):
            return "channel"
        return "channel"
    return type(ent).__name__


async def main():
    parser = argparse.ArgumentParser(description="List dialogs (groups/supergroups/channels/users) visible to this userbot")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--limit", type=int, default=0, help="Max dialogs to list (0 = no limit)")
    parser.add_argument("--search", help="Case-insensitive substring filter on title/username")
    parser.add_argument(
        "--kinds",
        help="Comma-separated kinds to include: user,group,supergroup,channel (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    kinds = None
    if args.kinds:
        kinds = {x.strip().lower() for x in str(args.kinds).split(",") if x.strip()}

    needle = str(args.search or "").lower().strip() if args.search else None

    config_manager = ConfigManager(args.config)
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

    out = []
    try:
        count = 0
        async for d in client.iter_dialogs(limit=int(args.limit) if int(args.limit) > 0 else None):
            ent = getattr(d, "entity", None)
            if ent is None:
                continue

            kind = _entity_kind(ent)
            if kinds and kind not in kinds:
                continue

            peer_id = None
            try:
                peer_id = int(utils.get_peer_id(ent))
            except Exception:  # noqa: BLE001
                peer_id = None

            title = getattr(ent, "title", None) or getattr(ent, "first_name", None) or getattr(ent, "username", None)
            title = str(title) if title else ""
            username = getattr(ent, "username", None)

            forum = bool(getattr(ent, "forum", False)) if isinstance(ent, types.Channel) else False
            megagroup = bool(getattr(ent, "megagroup", False)) if isinstance(ent, types.Channel) else False
            broadcast = bool(getattr(ent, "broadcast", False)) if isinstance(ent, types.Channel) else False

            if needle:
                hay = (title + " " + ("@" + username if username else "")).lower()
                if needle not in hay:
                    continue

            item = {
                "peer_id": peer_id,
                "kind": kind,
                "title": title,
                "username": f"@{username}" if username else None,
                "forum": forum,
                "megagroup": megagroup,
                "broadcast": broadcast,
            }

            if args.json:
                out.append(item)
            else:
                # tab-separated, easy to copy/paste
                print(
                    f"{item['peer_id']}\t{item['kind']}\tforum={int(item['forum'])}\t{item['title']}\t{item['username'] or ''}".rstrip()
                )

            count += 1

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
