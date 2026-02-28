import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, utils

from common_config import ConfigManager, load_userbot_settings


def _topic_title_variants(title: str) -> list[str]:
    if not title:
        return []

    t = str(title)
    out = [t]

    # If config topic names have a leading emoji ("🍑 Foo"), allow matching old titles.
    if " " in t:
        first, rest = t.split(" ", 1)
        if len(first) <= 8 and rest:
            out.append(rest)

    return out


def _iter_destinations(cfg: dict) -> list[dict]:
    relay = cfg.get("relay") or {}

    out: list[dict] = []

    for d in relay.get("default_destinations") or []:
        if isinstance(d, dict):
            out.append(d)

    for r in relay.get("routes") or []:
        for d in r.get("destinations") or r.get("dest_channels") or []:
            if isinstance(d, dict):
                out.append(d)

    return out


def _configured_topics_by_chat(cfg: dict) -> dict[int, set[str]]:
    relay = cfg.get("relay") or {}
    out: dict[int, set[str]] = {}

    mapping = relay.get("forum_topic_top_messages") or {}
    if isinstance(mapping, dict):
        for chat_key, topics in mapping.items():
            try:
                chat_id = int(chat_key)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(topics, dict):
                continue
            for title in topics.keys():
                if title:
                    out.setdefault(chat_id, set()).add(str(title))

    ensure = relay.get("ensure_forum_topics") or []
    if isinstance(ensure, list):
        for item in ensure:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            topics = item.get("topics") or []
            try:
                chat_id = int(chat_id)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(topics, list):
                continue
            for t in topics:
                if t:
                    out.setdefault(chat_id, set()).add(str(t))

    for d in _iter_destinations(cfg):
        chat_id = d.get("chat_id")
        if not chat_id:
            continue
        try:
            chat_id = int(chat_id)
        except Exception:  # noqa: BLE001
            continue

        title = d.get("topic_title") or d.get("topic")
        if title:
            out.setdefault(chat_id, set()).add(str(title))

        # topic_from_source / bucket_topics are dynamic; we can't statically check.

    return out


async def _get_forum_topics(client: TelegramClient, chat_id: int, *, limit: int = 100):
    entity = await client.get_entity(int(chat_id))
    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=int(limit),
            q="",
        )
    )
    topics = list(getattr(res, "topics", []) or [])
    return entity, topics


def _topic_state(topics: list, configured_title: str) -> tuple[str, dict | None]:
    """Return (state, topic_info)

    state:
      - ok
      - closed
      - missing

    topic_info: {title,id,top_message,closed,hidden}
    """

    # Exact or variant match by title.
    for variant in _topic_title_variants(configured_title):
        for t in topics:
            if str(getattr(t, "title", "")) == str(variant):
                info = {
                    "title": str(getattr(t, "title", "")),
                    "id": int(getattr(t, "id", 0) or 0),
                    "top_message": int(getattr(t, "top_message", 0) or 0),
                    "closed": bool(getattr(t, "closed", False)),
                    "hidden": bool(getattr(t, "hidden", False)),
                }
                if info["closed"]:
                    return "closed", info
                return "ok", info

    return "missing", None


def _prune_mapping(relay: dict, chat_id: int, titles_to_remove: set[str]) -> int:
    mapping = relay.get("forum_topic_top_messages") or {}
    removed = 0

    # mapping keys may be str(chat_id)
    for key in (str(chat_id), chat_id):
        if key not in mapping:
            continue
        chat_map = mapping.get(key)
        if not isinstance(chat_map, dict):
            continue
        for title in list(chat_map.keys()):
            if str(title) in titles_to_remove:
                del chat_map[title]
                removed += 1

        # if emptied, remove the chat entry
        if not chat_map:
            try:
                del mapping[key]
            except Exception:  # noqa: BLE001
                pass
        else:
            mapping[key] = chat_map

    relay["forum_topic_top_messages"] = mapping
    return removed


def _prune_ensure(relay: dict, chat_id: int, titles_to_remove: set[str], *, drop_empty_items: bool) -> int:
    ensure = relay.get("ensure_forum_topics") or []
    if not isinstance(ensure, list) or not ensure:
        return 0

    removed = 0
    new_ensure = []

    for item in ensure:
        if not isinstance(item, dict):
            new_ensure.append(item)
            continue

        try:
            cid = int(item.get("chat_id"))
        except Exception:  # noqa: BLE001
            new_ensure.append(item)
            continue

        if cid != int(chat_id):
            new_ensure.append(item)
            continue

        topics = item.get("topics") or []
        if not isinstance(topics, list):
            new_ensure.append(item)
            continue

        kept = []
        for t in topics:
            if t and str(t) in titles_to_remove:
                removed += 1
                continue
            kept.append(t)

        if kept or not drop_empty_items:
            item["topics"] = kept
            new_ensure.append(item)

    relay["ensure_forum_topics"] = new_ensure
    return removed


def _prune_destinations(relay: dict, chat_id: int, titles_to_remove: set[str], *, drop_empty_routes: bool) -> int:
    removed = 0

    def prune_list(dest_list: list) -> list:
        nonlocal removed
        out = []
        for d in dest_list or []:
            if not isinstance(d, dict):
                out.append(d)
                continue
            try:
                cid = int(d.get("chat_id"))
            except Exception:  # noqa: BLE001
                out.append(d)
                continue
            if cid != int(chat_id):
                out.append(d)
                continue

            title = d.get("topic_title") or d.get("topic")
            if title and str(title) in titles_to_remove:
                removed += 1
                continue

            out.append(d)
        return out

    relay["default_destinations"] = prune_list(relay.get("default_destinations") or [])

    routes = relay.get("routes") or []
    if isinstance(routes, list) and routes:
        new_routes = []
        for r in routes:
            if not isinstance(r, dict):
                new_routes.append(r)
                continue

            dests = r.get("destinations") or r.get("dest_channels") or []
            new_dests = prune_list(dests)

            if "destinations" in r:
                r["destinations"] = new_dests
            elif "dest_channels" in r:
                r["dest_channels"] = new_dests
            else:
                r["destinations"] = new_dests

            if new_dests or not drop_empty_routes:
                new_routes.append(r)

        relay["routes"] = new_routes

    return removed


def _prune_metadata(relay: dict, chat_id: int, titles_to_remove: set[str]) -> int:
    removed = 0

    for field in ("topic_renames", "topic_icon_emojis"):
        obj = relay.get(field) or {}
        if not isinstance(obj, dict):
            continue

        chat_obj = obj.get(str(chat_id)) or obj.get(chat_id)
        if not isinstance(chat_obj, dict):
            continue

        for k in list(chat_obj.keys()):
            # topic_renames: keys are old titles; values are new titles
            # topic_icon_emojis: keys are topic titles
            if str(k) in titles_to_remove or str(chat_obj.get(k)) in titles_to_remove:
                del chat_obj[k]
                removed += 1

        if not chat_obj:
            obj.pop(str(chat_id), None)
            obj.pop(chat_id, None)
        else:
            obj[str(chat_id)] = chat_obj

        relay[field] = obj

    return removed


async def main():
    parser = argparse.ArgumentParser(
        description=(
            "Detect closed/missing (deleted/renamed) forum topics for configured destination chats, and optionally prune them from config.json. "
            "This script NEVER creates topics."
        )
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")

    parser.add_argument("--topics-limit", type=int, default=100, help="Max topics to fetch per forum (default: 100)")

    parser.add_argument("--json", action="store_true", help="Output a machine-readable report")

    parser.add_argument("--prune-missing", action="store_true", help="Remove topics that are not found in the forum topic list")
    parser.add_argument("--prune-closed", action="store_true", help="Remove topics that exist but are marked closed")

    parser.add_argument(
        "--write",
        action="store_true",
        help="Write changes back to config.json (otherwise dry-run report only)",
    )

    parser.add_argument("--prune-mapping", action="store_true", help="Prune relay.forum_topic_top_messages")
    parser.add_argument("--prune-ensure", action="store_true", help="Prune relay.ensure_forum_topics")
    parser.add_argument(
        "--drop-empty-ensure-items",
        action="store_true",
        help="When pruning ensure_forum_topics, drop items that end up with no topics",
    )

    parser.add_argument(
        "--prune-destinations",
        action="store_true",
        help="Also prune destination entries in relay.default_destinations / relay.routes that reference removed topic titles",
    )
    parser.add_argument(
        "--drop-empty-routes",
        action="store_true",
        help="When pruning destinations, drop routes that end up with no destinations",
    )

    parser.add_argument(
        "--prune-metadata",
        action="store_true",
        help="Also prune relay.topic_renames / relay.topic_icon_emojis entries that reference removed topic titles",
    )

    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)
    relay = cfg.get("relay") or {}

    configured = _configured_topics_by_chat(cfg)
    if not configured:
        raise SystemExit("No configured destination topics found in config (forum_topic_top_messages / ensure_forum_topics / routes/default_destinations)")

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
        "config": str(config_manager.path),
        "chats": [],
        "summary": {"missing": 0, "closed": 0, "ok": 0, "errors": 0},
    }

    try:
        for chat_id, titles in sorted(configured.items(), key=lambda x: int(x[0])):
            chat_entry = {
                "chat_id": int(chat_id),
                "chat_title": None,
                "chat_username": None,
                "configured_topics": sorted(list(titles)),
                "topics": [],
                "missing": [],
                "closed": [],
                "ok": [],
                "error": None,
                "planned_removals": [],
            }

            try:
                entity, topics = await _get_forum_topics(client, int(chat_id), limit=int(args.topics_limit))
                chat_entry["chat_title"] = getattr(entity, "title", None) or getattr(entity, "username", None)
                chat_entry["chat_username"] = f"@{getattr(entity, 'username', None)}" if getattr(entity, "username", None) else None

                chat_entry["topics"] = [
                    {
                        "title": str(getattr(t, "title", "")),
                        "id": int(getattr(t, "id", 0) or 0),
                        "top_message": int(getattr(t, "top_message", 0) or 0),
                        "closed": bool(getattr(t, "closed", False)),
                        "hidden": bool(getattr(t, "hidden", False)),
                    }
                    for t in topics
                ]

                titles_to_remove: set[str] = set()

                for title in sorted(list(titles)):
                    state, info = _topic_state(topics, str(title))
                    if state == "ok":
                        chat_entry["ok"].append({"configured_title": str(title), "matched": info})
                        report["summary"]["ok"] += 1
                    elif state == "closed":
                        chat_entry["closed"].append({"configured_title": str(title), "matched": info})
                        report["summary"]["closed"] += 1
                        if args.prune_closed:
                            titles_to_remove.add(str(title))
                    else:
                        chat_entry["missing"].append({"configured_title": str(title)})
                        report["summary"]["missing"] += 1
                        if args.prune_missing:
                            titles_to_remove.add(str(title))

                chat_entry["planned_removals"] = sorted(list(titles_to_remove))

                if args.write and titles_to_remove:
                    changes = {"mapping": 0, "ensure": 0, "destinations": 0, "metadata": 0}

                    if args.prune_mapping:
                        changes["mapping"] = _prune_mapping(relay, int(chat_id), titles_to_remove)

                    if args.prune_ensure:
                        changes["ensure"] = _prune_ensure(
                            relay,
                            int(chat_id),
                            titles_to_remove,
                            drop_empty_items=bool(args.drop_empty_ensure_items),
                        )

                    if args.prune_destinations:
                        changes["destinations"] = _prune_destinations(
                            relay,
                            int(chat_id),
                            titles_to_remove,
                            drop_empty_routes=bool(args.drop_empty_routes),
                        )

                    if args.prune_metadata:
                        changes["metadata"] = _prune_metadata(relay, int(chat_id), titles_to_remove)

                    chat_entry["applied_changes"] = changes

            except Exception as exc:  # noqa: BLE001
                report["summary"]["errors"] += 1
                chat_entry["error"] = str(exc)

            report["chats"].append(chat_entry)

        if args.write:
            cfg["relay"] = relay
            config_manager.save(cfg)

        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        # human readable
        for c in report["chats"]:
            title = c.get("chat_title") or str(c["chat_id"])
            print(f"=== {title} (chat_id={c['chat_id']}{', ' + c['chat_username'] if c.get('chat_username') else ''}) ===")
            if c.get("error"):
                print(f"ERROR: {c['error']}")
                print()
                continue

            if c.get("planned_removals"):
                print(f"Planned removals: {', '.join(c['planned_removals'])}")

            if c.get("closed"):
                print("Closed:")
                for x in c["closed"]:
                    m = x.get("matched") or {}
                    print(f"  - {x['configured_title']} (matched={m.get('title')}, top_message={m.get('top_message')})")

            if c.get("missing"):
                print("Missing (deleted/renamed/not found):")
                for x in c["missing"]:
                    print(f"  - {x['configured_title']}")

            if c.get("ok"):
                print("OK:")
                for x in c["ok"]:
                    m = x.get("matched") or {}
                    suffix = " CLOSED" if m.get("closed") else ""
                    print(f"  - {x['configured_title']} (matched={m.get('title')}, top_message={m.get('top_message')}){suffix}")

            if c.get("applied_changes"):
                ch = c["applied_changes"]
                print(f"Applied: mapping={ch.get('mapping')} ensure={ch.get('ensure')} destinations={ch.get('destinations')} metadata={ch.get('metadata')}")

            print()

        print(
            "Summary: ok={ok} closed={closed} missing={missing} errors={errors}".format(
                **report["summary"]
            )
        )

        if not args.write:
            print("(dry-run) Use --write with --prune-missing/--prune-closed and --prune-mapping/--prune-ensure etc. to apply changes.")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
