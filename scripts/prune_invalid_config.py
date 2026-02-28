import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, types, utils
from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatIdInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)

from common_config import ConfigManager, load_userbot_settings


def _topic_title_variants(title: str) -> list[str]:
    if not title:
        return []
    t = str(title)
    out = [t]
    # "🍑 Foo" -> "Foo"
    if " " in t:
        first, rest = t.split(" ", 1)
        if len(first) <= 8 and rest:
            out.append(rest)
    return out


def _destinations_iter(relay: dict):
    # Yield (container_list, index, destination_dict)
    for container in (relay.get("default_destinations") or []):
        # default_destinations is a list; handled below by caller.
        pass


async def _safe_get_entity(client: TelegramClient, peer):
    try:
        return await client.get_entity(peer)
    except (ValueError, ChannelInvalidError, ChannelPrivateError, ChatIdInvalidError, UsernameInvalidError, UsernameNotOccupiedError) as exc:
        raise exc


async def _is_accessible(client: TelegramClient, peer) -> bool:
    # Fast check: try reading 1 message.
    try:
        await client.get_messages(peer, limit=1)
        return True
    except (ChannelPrivateError, ChatAdminRequiredError, UserNotParticipantError):
        return False
    except RPCError:
        return False
    except Exception:  # noqa: BLE001
        return False


async def _leave_peer(client: TelegramClient, ent) -> tuple[bool, str | None]:
    try:
        if isinstance(ent, types.Channel):
            await client(functions.channels.LeaveChannelRequest(channel=ent))
            return True, None
        if isinstance(ent, types.Chat):
            await client(functions.messages.DeleteChatUserRequest(chat_id=int(ent.id), user_id=types.InputUserSelf()))
            return True, None
        return False, "unsupported_entity_type"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def _list_forum_topics(client: TelegramClient, ent, *, limit: int = 200):
    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=ent,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=int(limit),
            q="",
        )
    )
    topics = list(getattr(res, "topics", []) or [])
    by_title = {t.title: t for t in topics}
    return topics, by_title


def _normalize_destinations(destinations) -> list[dict]:
    if not destinations:
        return []
    out = []
    for d in destinations:
        if isinstance(d, int):
            out.append({"chat_id": int(d)})
        elif isinstance(d, dict):
            out.append(d)
    return out


def _route_sources(route: dict) -> list[int]:
    if "source_chat" in route:
        try:
            return [int(route.get("source_chat"))]
        except Exception:  # noqa: BLE001
            return []

    srcs = route.get("source_chats")
    if isinstance(srcs, list):
        out: list[int] = []
        for x in srcs:
            try:
                out.append(int(x))
            except Exception:  # noqa: BLE001
                continue
        return out

    return []


def _collect_destination_chat_ids(relay: dict) -> set[int]:
    ids: set[int] = set()

    for d in _normalize_destinations(relay.get("default_destinations") or []):
        if d.get("chat_id") is not None:
            ids.add(int(d.get("chat_id")))

    for route in relay.get("routes") or []:
        raw = route.get("destinations") or route.get("dest_channels") or []
        for d in _normalize_destinations(raw):
            if d.get("chat_id") is not None:
                ids.add(int(d.get("chat_id")))

    # legacy
    for x in relay.get("dest_channels") or []:
        try:
            ids.add(int(x))
        except Exception:  # noqa: BLE001
            pass

    for chat_key in (relay.get("forum_topic_top_messages") or {}).keys():
        try:
            ids.add(int(chat_key))
        except Exception:  # noqa: BLE001
            pass

    for item in relay.get("ensure_forum_topics") or []:
        try:
            ids.add(int(item.get("chat_id")))
        except Exception:  # noqa: BLE001
            pass

    return ids


async def main():
    parser = argparse.ArgumentParser(
        description="Detect deleted/closed/inaccessible chats/topics referenced by config.json, optionally prune them and auto-leave. This script will NOT create topics."
    )

    parser.add_argument("--config", default="config.json", help="Path to config.json")

    parser.add_argument("--write", action="store_true", help="Write pruned config back to disk")
    parser.add_argument("--dry-run", action="store_true", help="Do not write/leave; only print what would change")

    parser.add_argument("--prune-bot-mappings", action="store_true", help="Prune invalid source chats in bot_mappings")
    parser.add_argument("--prune-destinations", action="store_true", help="Prune invalid destination chat_ids in relay routes/default_destinations")
    parser.add_argument(
        "--prune-topic-mapping",
        action="store_true",
        help="Prune invalid or closed topics in relay.forum_topic_top_messages",
    )
    parser.add_argument(
        "--prune-closed-topics",
        action="store_true",
        help="Treat closed topics as invalid (requires --prune-topic-mapping)",
    )

    parser.add_argument(
        "--leave",
        action="store_true",
        help="Attempt to leave chats that are removed from config (best-effort; uses userbot identity)",
    )

    parser.add_argument("--topics-limit", type=int, default=200, help="Max topics to fetch per forum chat (default: 200)")

    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    relay = cfg.get("relay") or {}

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

    report = {
        "bot_mappings_removed": [],
        "destinations_removed": [],
        "topic_mapping_removed": [],
        "left": [],
        "errors": [],
    }

    changed = False

    try:
        # A) prune bot_mappings
        if args.prune_bot_mappings:
            new_mappings = []
            for m in cfg.get("bot_mappings") or []:
                src = m.get("source_chat")
                if src is None:
                    continue

                # source_chat can be int-ish or username.
                peer = None
                try:
                    peer = int(src)
                except Exception:  # noqa: BLE001
                    peer = str(src)

                ent = None
                ok = True
                reason = None

                try:
                    ent = await _safe_get_entity(client, peer)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    reason = f"get_entity_failed: {exc}"

                if ok and ent is not None:
                    accessible = await _is_accessible(client, ent)
                    if not accessible:
                        ok = False
                        reason = "inaccessible_or_not_participant"

                if ok:
                    new_mappings.append(m)
                    continue

                report["bot_mappings_removed"].append({"source_chat": src, "target_bot": m.get("target_bot"), "reason": reason})
                changed = True

                if args.leave and ent is not None and not args.dry_run:
                    left_ok, left_err = await _leave_peer(client, ent)
                    report["left"].append({"peer_id": utils.get_peer_id(ent), "title": getattr(ent, "title", None), "ok": left_ok, "error": left_err})

            if new_mappings != (cfg.get("bot_mappings") or []):
                cfg["bot_mappings"] = new_mappings

        # B) prune destinations in relay
        if args.prune_destinations:
            invalid_chat_ids: set[int] = set()
            all_dest_chat_ids = _collect_destination_chat_ids(relay)

            for chat_id in sorted(all_dest_chat_ids):
                ent = None
                try:
                    ent = await _safe_get_entity(client, int(chat_id))
                    # Note: destinations can be channels you administer; still check accessibility.
                    if not await _is_accessible(client, ent):
                        raise ChannelPrivateError(request=None)  # mark as inaccessible
                except Exception as exc:  # noqa: BLE001
                    invalid_chat_ids.add(int(chat_id))
                    report["destinations_removed"].append({"chat_id": int(chat_id), "reason": str(exc)})
                    changed = True
                    if args.leave and ent is not None and not args.dry_run:
                        left_ok, left_err = await _leave_peer(client, ent)
                        report["left"].append({"peer_id": utils.get_peer_id(ent), "title": getattr(ent, "title", None), "ok": left_ok, "error": left_err})

            def _prune_dest_list(lst):
                out = []
                for d in _normalize_destinations(lst):
                    cid = d.get("chat_id")
                    if cid is None:
                        continue
                    if int(cid) in invalid_chat_ids:
                        continue
                    out.append(d)
                return out

            relay["default_destinations"] = _prune_dest_list(relay.get("default_destinations") or [])

            routes_new = []
            for route in relay.get("routes") or []:
                raw = route.get("destinations") or route.get("dest_channels") or []
                pruned = _prune_dest_list(raw)
                if route.get("destinations") is not None:
                    route["destinations"] = pruned
                elif route.get("dest_channels") is not None:
                    route["dest_channels"] = pruned
                else:
                    route["destinations"] = pruned

                # If a route ends up with no destinations, drop it.
                if not pruned:
                    continue
                routes_new.append(route)

            relay["routes"] = routes_new

            # legacy
            relay["dest_channels"] = [x for x in (relay.get("dest_channels") or []) if int(x) not in invalid_chat_ids]

            # ensure_forum_topics
            ensure_new = []
            for item in relay.get("ensure_forum_topics") or []:
                cid = item.get("chat_id")
                if cid is None:
                    continue
                try:
                    if int(cid) in invalid_chat_ids:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                ensure_new.append(item)
            relay["ensure_forum_topics"] = ensure_new

            # forum_topic_top_messages
            ft = relay.get("forum_topic_top_messages") or {}
            ft_new = {}
            for chat_key, mapping in ft.items():
                try:
                    cid = int(chat_key)
                except Exception:  # noqa: BLE001
                    continue
                if cid in invalid_chat_ids:
                    continue
                ft_new[str(cid)] = mapping
            relay["forum_topic_top_messages"] = ft_new

            cfg["relay"] = relay

        # C) prune forum topic mapping
        if args.prune_topic_mapping:
            ft = relay.get("forum_topic_top_messages") or {}
            ft_new: dict[str, dict[str, int]] = {}

            for chat_key, mapping in ft.items():
                try:
                    chat_id = int(chat_key)
                except Exception:  # noqa: BLE001
                    continue

                if not isinstance(mapping, dict):
                    continue

                ent = None
                try:
                    ent = await _safe_get_entity(client, chat_id)
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append({"chat_id": chat_id, "error": f"get_entity_failed: {exc}"})
                    changed = True
                    continue

                # If it's not a forum-enabled supergroup, keep mapping untouched (or drop?).
                if not (isinstance(ent, types.Channel) and bool(getattr(ent, "forum", False))):
                    ft_new[str(chat_id)] = {str(k): int(v) for k, v in mapping.items() if k and v}
                    continue

                try:
                    topics, by_title = await _list_forum_topics(client, ent, limit=int(args.topics_limit))
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append({"chat_id": chat_id, "error": f"list_topics_failed: {exc}"})
                    # Keep existing mapping if we couldn't list.
                    ft_new[str(chat_id)] = {str(k): int(v) for k, v in mapping.items() if k and v}
                    continue

                # Build a title->topic lookup that supports emoji-prefix variants.
                variant_to_topic = {}
                for t in topics:
                    for v in _topic_title_variants(t.title):
                        variant_to_topic.setdefault(str(v), t)

                kept: dict[str, int] = {}
                for title, top_message in mapping.items():
                    if not title:
                        continue

                    matched = None
                    for v in _topic_title_variants(str(title)):
                        matched = variant_to_topic.get(str(v))
                        if matched is not None:
                            break

                    if matched is None:
                        report["topic_mapping_removed"].append({"chat_id": chat_id, "topic": str(title), "reason": "topic_missing"})
                        changed = True
                        continue

                    if args.prune_closed_topics and bool(getattr(matched, "closed", False)):
                        report["topic_mapping_removed"].append({"chat_id": chat_id, "topic": str(title), "reason": "topic_closed"})
                        changed = True
                        continue

                    kept[str(matched.title)] = int(getattr(matched, "top_message", top_message))

                ft_new[str(chat_id)] = kept

            relay["forum_topic_top_messages"] = ft_new
            cfg["relay"] = relay

        # Save/report
        print(json.dumps({"changed": bool(changed), **report}, ensure_ascii=False, indent=2))

        if changed and args.write and not args.dry_run:
            config_manager.save(cfg)

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
