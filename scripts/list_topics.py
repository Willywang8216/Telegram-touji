#!/usr/bin/env python3
"""List Telegram forum topics (Topics-enabled supergroups).

Usage:
  python scripts/list_topics.py --peer <peer_id or @username> [--session anon] [--json topics.json]

This script mirrors the auth/bootstrap logic from scripts/list_dialogs.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = str(Path(__file__).resolve().parents[1])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import argparse
import asyncio
import json
import os
from datetime import datetime
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import RPCError
from telethon.tl.functions.messages import GetForumTopicsRequest

from common_config import ConfigManager, load_userbot_settings


def _safe_str(value: Any) -> str:
    return str(value).replace("\t", " ").replace("\n", " ").strip()


def _parse_peer(peer: str) -> str | int:
    peer = peer.strip()
    if peer.startswith("@"):
        return peer
    try:
        return int(peer)
    except ValueError:
        return peer


async def main() -> None:
    parser = argparse.ArgumentParser(description="List Telegram forum topics for a given supergroup")
    parser.add_argument(
        "--peer",
        required=True,
        help="Peer id (from list_dialogs.py) or @username",
    )
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
        config_manager = ConfigManager(args.config)
        settings = load_userbot_settings(config_manager)
        api_id = int(settings["api_id"])
        api_hash = str(settings["api_hash"])
        proxy = settings["proxy"]
        client = TelegramClient(args.session, api_id, api_hash, proxy=proxy)

    await client.start()

    peer_ref = _parse_peer(args.peer)
    input_peer = await client.get_input_entity(peer_ref)

    topics_rows: list[dict[str, Any]] = []
    try:
        result = await client(
            GetForumTopicsRequest(
                peer=input_peer,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=100,
                q=None,
            )
        )

        topics = list(getattr(result, "topics", []) or [])
        if not topics:
            print("This chat does not have Topics enabled (Forum)")
        else:
            for idx, t in enumerate(topics, start=1):
                row = {
                    "id": getattr(t, "id", None),
                    "title": _safe_str(getattr(t, "title", "")),
                    "top_message": getattr(t, "top_message", None),
                }
                topics_rows.append(row)
                print(
                    f"[{idx}] id={row['id']}  title={row['title']}  top_message={row['top_message']}"
                )
    except RPCError:
        print("This chat does not have Topics enabled (Forum)")

    if args.json:
        out_path = Path(args.json)
        payload = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "peer": {
                "input": args.peer,
                "peer_id": utils.get_peer_id(input_peer),
            },
            "topics": topics_rows,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote JSON: {out_path}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
