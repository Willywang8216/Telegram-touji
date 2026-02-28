import argparse
import asyncio
import re
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, types

from common_config import ConfigManager, load_userbot_settings


_INVITE_RE = re.compile(r"(?:joinchat/|\+)([A-Za-z0-9_-]+)")


def _parse_peer(raw: str):
    raw = str(raw).strip()

    # Support invite links like https://t.me/+HASH or https://t.me/joinchat/HASH
    m = _INVITE_RE.search(raw)
    if m:
        return {"invite_hash": m.group(1)}

    try:
        return {"peer": int(raw)}
    except ValueError:
        return {"peer": raw}


async def _leave(client: TelegramClient, ent):
    if isinstance(ent, types.Channel):
        await client(functions.channels.LeaveChannelRequest(channel=ent))
        return
    if isinstance(ent, types.Chat):
        await client(functions.messages.DeleteChatUserRequest(chat_id=int(ent.id), user_id=types.InputUserSelf()))
        return
    raise ValueError(f"Unsupported entity type: {type(ent).__name__}")


async def main():
    parser = argparse.ArgumentParser(description="Join/leave a channel/supergroup/group using the userbot account")
    parser.add_argument("--config", default="config.json", help="Path to config.json")

    act = parser.add_mutually_exclusive_group(required=True)
    act.add_argument("--join", action="store_true", help="Join the given peer")
    act.add_argument("--leave", action="store_true", help="Leave the given peer")

    parser.add_argument("--peer", required=True, help="@username, -100..., or invite link (t.me/+HASH)")
    args = parser.parse_args()

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

    try:
        parsed = _parse_peer(args.peer)

        if args.join:
            if "invite_hash" in parsed:
                await client(functions.messages.ImportChatInviteRequest(hash=parsed["invite_hash"]))
                return
            ent = await client.get_entity(parsed["peer"])
            await client(functions.channels.JoinChannelRequest(channel=ent))
            return

        # leave
        ent = await client.get_entity(parsed.get("peer") or args.peer)
        await _leave(client, ent)

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
