#!/usr/bin/env python3
"""List Telegram dialogs (channels/groups) you joined and print their Telethon peer_id.

You can use the printed peer_id values in this repo's config.json:
- bot_mappings[].source_chat
- relay.dest_channels[]

By default it prints only channels/groups. Use --all to include users/bots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils

from common_config import ConfigManager, load_userbot_settings


def _classify(dialog, entity: Any) -> str:
    # Dialog flags are the most reliable.
    if getattr(dialog, "is_channel", False):
        if getattr(entity, "broadcast", False):
            return "channel"
        if getattr(entity, "megagroup", False):
            return "supergroup"
        return "channel"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_user", False):
        return "user"
    return "other"


def _role_flags(entity: Any) -> tuple[bool, bool]:
    # These attributes are not always present, but often are for Chat/Channel.
    creator = bool(getattr(entity, "creator", False))
    admin = getattr(entity, "admin_rights", None) is not None
    return creator, admin


def _safe_str(value: Any) -> str:
    return str(value).replace("\t", " ").replace("\n", " ").strip()


async def main() -> None:
    parser = argparse.ArgumentParser(description="List Telegram dialogs and their peer_id")
    parser.add_argument(
        "--session",
        default="anon",
        help="Telethon session name (default: anon)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Config path (default: CONFIG_PATH env or ./config.json)",
    )
    parser.add_argument(
        "--api-id",
        type=int,
        default=None,
        help="Override API_ID (useful if config.json not created yet)",
    )
    parser.add_argument(
        "--api-hash",
        default=None,
        help="Override API_HASH (useful if config.json not created yet)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include users/bots (default: only channels/groups)",
    )
    parser.add_argument(
        "--json",
        default="",
        help="Write JSON output to this path (optional)",
    )
    args = parser.parse_args()

    # Prefer CLI overrides, then env, then config.
    proxy = None
    api_id = args.api_id or (int(os.environ["API_ID"]) if os.getenv("API_ID") else None)
    api_hash = args.api_hash or os.getenv("API_HASH")

    if api_id and api_hash:
        client = TelegramClient(args.session, api_id, api_hash)
    else:
        # Fall back to repo config (.env + config.json).
        config_manager = ConfigManager(args.config)
        settings = load_userbot_settings(config_manager)
        api_id = int(settings["api_id"])
        api_hash = str(settings["api_hash"])
        proxy = settings["proxy"]
        client = TelegramClient(args.session, api_id, api_hash, proxy=proxy)
    await client.start()

    me = await client.get_me()

    rows: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        kind = _classify(dialog, entity)

        if not args.all and kind not in {"channel", "supergroup", "group"}:
            continue

        peer_id = utils.get_peer_id(entity)
        title = _safe_str(getattr(entity, "title", None) or getattr(dialog, "name", None) or peer_id)
        username = getattr(entity, "username", None)
        username = f"@{username}" if username else None

        creator, admin = _role_flags(entity)

        rows.append(
            {
                "title": title,
                "username": username,
                "kind": kind,
                "peer_id": peer_id,
                "raw_id": getattr(entity, "id", None),
                "creator": creator,
                "admin": admin,
            }
        )

    rows.sort(key=lambda r: (r["kind"], (r["title"] or "").lower()))

    print("\n=== Telegram dialogs (use peer_id in config.json) ===\n")
    print(f"my_user_id={me.id}\n")

    for r in rows:
        role_bits = []
        if r["creator"]:
            role_bits.append("creator")
        if r["admin"]:
            role_bits.append("admin")
        role = f" ({', '.join(role_bits)})" if role_bits else ""
        uname = f" {r['username']}" if r.get("username") else ""
        print(f"[{r['kind']:<10}] peer_id={r['peer_id']}  title={r['title']}{uname}{role}")

    if args.json:
        out_path = Path(args.json)
        payload = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "my_user_id": me.id,
            "count": len(rows),
            "dialogs": rows,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote JSON: {out_path}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
