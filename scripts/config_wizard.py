#!/usr/bin/env python3
"""Interactive wizard to generate config.json + .env for this repo.

This helps you fill the *existing* config fields used by:
- telegram_bot.py (Userbot): api_id, api_hash, master_account_id, bot_mappings
- bot_relay.py (RelayBot): relay.bot_token, relay.dest_channels

If you first run `scripts/list_dialogs.py --json dialogs.json`, this wizard can
load it and let you select sources/destinations by number.

Notes:
- Current relay logic broadcasts to a single `relay.dest_channels` list.
  (Per-source routing is a future enhancement.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = str(Path(__file__).resolve().parents[1])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import argparse
import json
import os
from getpass import getpass
from typing import Any


def _parse_int(value: str, *, field: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise SystemExit(f"Invalid integer for {field}: {value!r}") from exc


def _parse_source(value: str) -> int | str:
    v = value.strip()
    if v.lstrip("-").isdigit():
        return int(v)
    return v


def _load_existing_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _prompt(text: str, *, default: str | None = None) -> str:
    suffix = f" (default: {default}): " if default is not None else ": "
    raw = input(text + suffix).strip()
    return raw or (default or "")


def _prompt_secret(text: str, *, default: str | None = None) -> str:
    # getpass hides the input.
    suffix = f" (default: {default}): " if default is not None else ": "
    raw = getpass(text + suffix).strip()
    return raw or (default or "")


def _prompt_yes_no(text: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{text} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Please answer y or n")


def _parse_range_list(raw: str, *, n: int) -> list[int]:
    """Parse 1-based selections like: 1,2,5-7 or 'all'."""

    raw = raw.strip().lower()
    if raw in {"all", "*"}:
        return list(range(1, n + 1))

    out: set[int] = set()
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if "-" in part:
            a_str, b_str = [x.strip() for x in part.split("-", 1)]
            a = _parse_int(a_str, field="range start")
            b = _parse_int(b_str, field="range end")
            if a > b:
                a, b = b, a
            out.update(range(a, b + 1))
        else:
            out.add(_parse_int(part, field="selection"))

    bad = [i for i in out if i < 1 or i > n]
    if bad:
        raise SystemExit(f"Selection out of range (1..{n}): {bad}")

    return sorted(out)


def _load_dialogs(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("dialogs"), list):
        return [d for d in payload["dialogs"] if isinstance(d, dict)]
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    raise SystemExit(f"Unsupported dialogs JSON format: {path}")


def _print_dialogs(dialogs: list[dict[str, Any]]) -> None:
    for i, d in enumerate(dialogs, start=1):
        title = d.get("title") or ""
        username = d.get("username") or ""
        kind = d.get("kind") or ""
        peer_id = d.get("peer_id")
        print(f"{i:>3}. [{kind:<10}] peer_id={peer_id}  {title} {username}".rstrip())


def _select_dialog_ids(dialogs: list[dict[str, Any]], *, prompt: str) -> list[int]:
    if not dialogs:
        return []

    _print_dialogs(dialogs)
    raw = _prompt(prompt, default="")
    if not raw:
        return []

    idxs = _parse_range_list(raw, n=len(dialogs))
    out: list[int] = []
    for i in idxs:
        peer_id = dialogs[i - 1].get("peer_id")
        if isinstance(peer_id, int):
            out.append(peer_id)
        elif isinstance(peer_id, str) and peer_id.strip().lstrip("-").isdigit():
            out.append(int(peer_id.strip()))
    return out


def _maybe_overwrite(path: Path, *, force: bool) -> None:
    if force or not path.exists():
        return
    if not _prompt_yes_no(f"{path} exists. Overwrite?", default=False):
        raise SystemExit("Aborted.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive config.json + .env generator")
    parser.add_argument("--dialogs", default="", help="Optional dialogs.json path from list_dialogs.py")
    parser.add_argument("--config", default="config.json", help="Output config.json path")
    parser.add_argument("--env", default=".env", help="Output .env path")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    config_path = Path(args.config)
    env_path = Path(args.env)

    existing = _load_existing_config(config_path)

    api_id_default = os.getenv("API_ID") or str(existing.get("api_id") or "12345678")
    api_hash_default = os.getenv("API_HASH") or str(existing.get("api_hash") or "")
    master_default = os.getenv("MASTER_ACCOUNT_ID") or str(existing.get("master_account_id") or "123456789")

    api_id = _parse_int(_prompt("api_id", default=api_id_default), field="api_id")
    api_hash = _prompt_secret("api_hash", default=api_hash_default or "your_api_hash")
    master_account_id = _parse_int(_prompt("master_account_id", default=master_default), field="master_account_id")

    dialogs_path = Path(args.dialogs) if args.dialogs else None
    dialogs: list[dict[str, Any]] = []
    if dialogs_path:
        if not dialogs_path.exists():
            raise SystemExit(f"dialogs.json not found: {dialogs_path}")
        dialogs = _load_dialogs(dialogs_path)

    # Filter only groups/channels in dialogs.json (as generated by list_dialogs.py)
    dialog_gc = [d for d in dialogs if d.get("kind") in {"channel", "supergroup", "group"}]

    print("\nSelect source chats (the Userbot will LISTEN these):")
    source_ids = _select_dialog_ids(dialog_gc, prompt="Select numbers (e.g. 1,2,5-7) or empty to type manually")
    if not source_ids:
        raw_sources = _prompt("source_chat IDs/usernames (comma-separated)", default="-1001234567890")
        sources = [_parse_source(x) for x in raw_sources.split(",") if x.strip()]
    else:
        sources = source_ids

    target_bot_default = ""
    if isinstance(existing.get("bot_mappings"), list) and existing["bot_mappings"]:
        target_bot_default = str(existing["bot_mappings"][0].get("target_bot") or "")
    target_bot = _prompt("target_bot (@bot_username)", default=target_bot_default or "@your_middle_bot")

    bot_mappings = [{"source_chat": src, "target_bot": target_bot} for src in sources]

    relay = existing.get("relay") if isinstance(existing.get("relay"), dict) else {}
    relay_token_default = os.getenv("RELAY_BOT_TOKEN") or str(relay.get("bot_token") or "")
    relay_bot_token = _prompt_secret("relay.bot_token", default=relay_token_default or "123456:ABCDEF_your_bot_token")

    print("\nSelect relay destination channels/groups (RelayBot will SEND to ALL of them):")
    dest_ids = _select_dialog_ids(dialog_gc, prompt="Select numbers (or empty to type manually)")
    if not dest_ids:
        raw_dest = _prompt("relay.dest_channels IDs (comma-separated)", default="-1009876543210")
        dest_channels = [int(x.strip()) for x in raw_dest.split(",") if x.strip()]
    else:
        dest_channels = dest_ids

    cfg = {
        "api_id": api_id,
        "api_hash": api_hash,
        "master_account_id": master_account_id,
        "bot_mappings": bot_mappings,
        "relay": {
            "api_id": api_id,
            "api_hash": api_hash,
            "bot_token": relay_bot_token,
            "dest_channels": dest_channels,
        },
        "proxy": None,
    }

    _maybe_overwrite(config_path, force=args.force)
    _maybe_overwrite(env_path, force=args.force)

    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    env_lines = [
        f"API_ID={api_id}",
        f"API_HASH={api_hash}",
        f"MASTER_ACCOUNT_ID={master_account_id}",
        f"RELAY_API_ID={api_id}",
        f"RELAY_API_HASH={api_hash}",
        f"RELAY_BOT_TOKEN={relay_bot_token}",
        f"RELAY_DEST_CHANNELS={','.join(str(x) for x in dest_channels)}",
        "",
    ]
    env_path.write_text("\n".join(env_lines), encoding="utf-8")

    print("\nWrote:")
    print(f"  - {config_path}")
    print(f"  - {env_path}")
    print("\nNext:")
    print("  docker compose up -d --build")
    print("  docker compose logs -f")


if __name__ == "__main__":
    main()
